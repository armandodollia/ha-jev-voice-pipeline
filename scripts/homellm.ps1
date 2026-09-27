# Home LLM supervisor: keeps exactly one llama-server running (OpenAI API + Jev-mode /v1/decision,
# model alias from config) and picks the model from what the PC is doing:
#   full   - idle
#   stream - Apollo/Sunshine stream active (flag written by apollo-stream.ps1)
#   game   - a game is running locally (Steam RunningAppID, or a process under a game library folder)
# Smaller models load immediately; bigger ones only after the trigger has been gone for upgradeDelaySec.
# Modes that use the same model and arguments switch without restarting the server.
# Runs at boot as the SYSTEM scheduled task "HomeLLM" (see install.ps1).
param([switch]$DryRun)   # -DryRun: print detected game folders and the mode that would be chosen, then exit

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"
$cfg   = Read-KitConfig
$paths = Get-KitPaths
New-Item -ItemType Directory -Force $paths.State, $paths.Logs | Out-Null

$PollSeconds  = 5
$Rank         = @{ game = 0; stream = 1; full = 2 }   # lower = smaller model
$StreamFlag   = Join-Path $paths.State 'streaming.flag'
$OverrideFile = Join-Path $paths.State 'override.txt'   # optional: full|stream|game (anything else = auto)
$StatusFile   = Join-Path $paths.State 'status.json'
$ExtraGames   = Join-Path $paths.Root 'games.txt'
$IgnoreFile   = Join-Path $paths.Root 'ignore.txt'

function Write-Log($msg) { Write-KitLog 'supervisor.log' $msg }

function Get-ModeArgs($mode) {
    $m = $cfg.llm.modes.$mode
    $a = @('-m', "`"$(Join-Path $paths.Models $m.file)`"",
           '--host', '0.0.0.0', '--port', "$($cfg.llm.port)", '--alias', $cfg.llm.alias) + @($cfg.llm.commonArgs)
    if ($m.ctx)                { $a += '-c', "$($m.ctx)" }
    if ($m.decisionSeqs)       { $a += '--decision-seqs', "$($m.decisionSeqs)" }
    if ($m.chatTemplateKwargs) { $a += '--chat-template-kwargs', (ConvertTo-NativeJsonArg $m.chatTemplateKwargs) }
    if ($m.extraArgs)          { $a += @($m.extraArgs) }
    $a
}

function Read-List($path) {
    if (Test-Path $path) {
        Get-Content $path | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ -and -not $_.StartsWith('#') }
    }
}

function Get-SteamKeys {
    # This runs as SYSTEM, so look at every loaded user hive rather than HKCU.
    foreach ($hive in Get-ChildItem 'Registry::HKEY_USERS' -ErrorAction SilentlyContinue) {
        $k = Get-ItemProperty "Registry::$($hive.Name)\Software\Valve\Steam" -ErrorAction SilentlyContinue
        if ($k) { $k }
    }
}

function Get-GameRoots {
    $roots = @()
    foreach ($steam in Get-SteamKeys) {
        if (-not $steam.SteamPath) { continue }
        $vdf = Join-Path $steam.SteamPath 'steamapps\libraryfolders.vdf'
        if (Test-Path $vdf) {
            foreach ($m in Select-String -Path $vdf -Pattern '"path"\s+"([^"]+)"') {
                # String concat, not Join-Path: libraries on disconnected drives must not throw.
                $roots += ($m.Matches[0].Groups[1].Value -replace '\\\\', '\').TrimEnd('\') + '\steamapps\common'
            }
        }
    }
    $roots += 'C:\Program Files\Epic Games', 'C:\Program Files\EA Games',
              'C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\games', 'C:\XboxGames'
    $roots | Where-Object { Test-Path $_ } | ForEach-Object { $_.TrimEnd('\').ToLower() + '\' } | Select-Object -Unique
}

function Find-RunningGame($roots) {
    foreach ($steam in Get-SteamKeys) {
        if ($steam.RunningAppID -and $steam.RunningAppID -ne 0) { return "steam app $($steam.RunningAppID)" }
    }
    $extra  = @(Read-List $ExtraGames)
    $ignore = @(Read-List $IgnoreFile)
    foreach ($p in Get-Process -ErrorAction SilentlyContinue) {
        $exe = "$($p.ProcessName).exe".ToLower()
        if ($ignore -contains $exe) { continue }
        if ($extra -contains $exe) { return $exe }
        $path = try { $p.Path } catch { $null }
        if (-not $path) { continue }
        $path = $path.ToLower()
        foreach ($r in $roots) { if ($path.StartsWith($r)) { return $path } }
    }
    $null
}

function Get-DesiredMode($roots) {
    if (Test-Path $OverrideFile) {
        $o = (Get-Content $OverrideFile -Raw).Trim().ToLower()
        if ($Rank.ContainsKey($o)) { return @($o, 'manual override') }
    }
    $svc = Get-Service $cfg.apollo.serviceName -ErrorAction SilentlyContinue
    if ((Test-Path $StreamFlag) -and ($null -eq $svc -or $svc.Status -eq 'Running')) {
        return @('stream', 'stream active')
    }
    $game = Find-RunningGame $roots
    if ($game) { return @('game', $game) }
    @('full', 'idle')
}

function Stop-Server {
    Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $paths.Server } | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        $_.WaitForExit(15000) | Out-Null
    }
}

function Start-Server($mode) {
    Write-Log "starting $mode ($($cfg.llm.modes.$mode.file))"
    Start-Process -FilePath $paths.Server -ArgumentList (Get-ModeArgs $mode) -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $paths.Logs "server-$mode.out.log") `
        -RedirectStandardError  (Join-Path $paths.Logs "server-$mode.err.log")
}

function Test-SameServer($a, $b) {
    (($cfg.llm.modes.$a.file) -eq ($cfg.llm.modes.$b.file)) -and ((Get-ModeArgs $a) -join ' ') -eq ((Get-ModeArgs $b) -join ' ')
}

$roots = Get-GameRoots
if ($DryRun) {
    "game folders:"; $roots | ForEach-Object { "  $_" }
    $d, $r = Get-DesiredMode $roots
    "desired mode: $d ($r)"
    foreach ($m in 'full', 'stream', 'game') { "$m args: $((Get-ModeArgs $m) -join ' ')" }
    return
}

Write-Log 'supervisor started'
Write-Log "game folders: $($roots -join '; ')"
Stop-Server
$current = $null; $proc = $null; $since = Get-Date
$upgradeFrom = $null; $failures = 0; $rootsAge = Get-Date

while ($true) {
    try {
        if (((Get-Date) - $rootsAge).TotalMinutes -ge 30) { $roots = Get-GameRoots; $rootsAge = Get-Date }
        $desired, $reason = Get-DesiredMode $roots

        $switch = $false
        if ($null -eq $current) { $switch = $true }
        elseif ($desired -ne $current) {
            if ($Rank[$desired] -lt $Rank[$current] -or $reason -eq 'manual override') { $switch = $true }
            elseif (-not $upgradeFrom) { $upgradeFrom = Get-Date }
            elseif (((Get-Date) - $upgradeFrom).TotalSeconds -ge $cfg.llm.upgradeDelaySec) { $switch = $true }
        }
        if ($desired -eq $current -or $switch) { $upgradeFrom = $null }

        if ($switch) {
            Write-Log "mode $current -> $desired ($reason)"
            if (-not ($current -and $proc -and -not $proc.HasExited -and (Test-SameServer $current $desired))) {
                Stop-Server
                $proc = Start-Server $desired
            }
            $current = $desired; $since = Get-Date; $failures = 0
        }
        elseif ($proc -and $proc.HasExited) {
            $failures++
            Write-Log "server exited with code $($proc.ExitCode) (failure $failures), restarting"
            Start-Sleep -Seconds ([math]::Min(60, 5 * $failures))
            $proc = Start-Server $current
        }

        @{ mode = $current; model = $cfg.llm.modes.$current.file; reason = $reason; since = $since.ToString('o')
           pending = $(if ($upgradeFrom) { $desired } else { $null }) } | ConvertTo-Json | Set-Content $StatusFile
    }
    catch { Write-Log "error: $_" }
    Start-Sleep -Seconds $PollSeconds
}
