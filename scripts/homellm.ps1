# Home LLM supervisor: keeps at most one llama-server running (OpenAI API + Jev-mode /v1/decision, model alias from
# config) and sizes what runs on the GPU by how much VRAM everything else uses ("others": games, stream encoder,
# desktop, other GPU jobs). Tiers come from config.json llm.tiers, best first; a tier is allowed while others stays
# below its maxOthersMiB. Defaults:
#   full  (others < 10 GB) Gemma 4 12B + Whisper
#   small (others < 14.5 GB) Qwen3.5 4B + Whisper
#   voice (others < 19 GB) Whisper only
#   none  (above)          nothing; Whisper stops too
# "others" = total GPU memory in use minus llama-server and Whisper, from Windows' per-process GPU memory counters
# (nvidia-smi can't report per-process memory under WDDM). Downgrades happen at once; upgrades only after others has
# stayed vram.upgradeMarginMiB under the tier's limit for upgradeDelaySec and the bigger tier fits. voice.ps1 follows
# gpu_released in state\status.json. state\override.txt (full|small|voice|none, or any tier name) pins a tier and
# ignores VRAM until removed. The default limits leave ~3 GB free at each tier's edge for a game to grow into
# during the few seconds an unload takes. When a game/stream starts, the tier drops to llm.activityMaxTier at once
# (default small), and polling runs every llm.activityPollSec (default 2 s) until it ends.
# While a game or stream runs (llm.noLoadsDuringActivity, default on), no model is ever STARTED: unloads still happen
# at once, but the smaller tier's model, upgrades and crash restarts wait until the activity ends. Loading a model
# mid-game triggered GPU driver resets (TDR) on an RTX 4090 9-16 s after the load began. Outside games, a downgrade
# unloads at once and loads the smaller model llm.downgradeLoadDelaySec later.
# Runs at boot as the SYSTEM scheduled task "HomeLLM" (see install.ps1).
param([switch]$DryRun)   # -DryRun: print tiers, VRAM, detected game folders and the tier that would be chosen

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"
$cfg   = Read-KitConfig
$paths = Get-KitPaths
New-Item -ItemType Directory -Force $paths.State, $paths.Logs | Out-Null

$PollSeconds  = 5
$BusyPoll     = $(if ($cfg.llm.activityPollSec) { [int]$cfg.llm.activityPollSec } else { 2 })
$StreamFlag   = Join-Path $paths.State 'streaming.flag'
$OverrideFile = Join-Path $paths.State 'override.txt'
$StatusFile   = Join-Path $paths.State 'status.json'
$ExtraGames   = Join-Path $paths.Root 'games.txt'
$IgnoreFile   = Join-Path $paths.Root 'ignore.txt'
$Margin       = $(if ($cfg.vram.upgradeMarginMiB) { [int]$cfg.vram.upgradeMarginMiB } else { 1536 })
$Headroom     = $(if ($cfg.vram.headroomMiB) { [int]$cfg.vram.headroomMiB } else { 1024 })
$LoadDelay    = $(if ($null -ne $cfg.llm.downgradeLoadDelaySec) { [int]$cfg.llm.downgradeLoadDelaySec } else { 60 })
$NoBusyLoads  = $(if ($null -ne $cfg.llm.noLoadsDuringActivity) { [bool]$cfg.llm.noLoadsDuringActivity } else { $true })
# Multi-PC (config cluster.role): an orchestrator's llama-server moves to cluster.localPorts.llm because router.py takes
# the public port; a worker loads models only while the orchestrator asks (GET <orchestrator>/assignment).
$Role       = "$($cfg.cluster.role)"
$IsWorker   = $Role -eq 'worker'
$ServerPort = $(if ($Role -eq 'orchestrator' -and $cfg.cluster.localPorts.llm) { [int]$cfg.cluster.localPorts.llm } else { [int]$cfg.llm.port })
$UpDelay    = $(if ($IsWorker) { 5 } else { $cfg.llm.upgradeDelaySec })   # a worker answers requests at once
$Assign     = [pscustomobject]@{ llm = $false; whisper = $false }
$AssignOkAt = Get-Date

function Write-Log($msg) { Write-KitLog 'supervisor.log' $msg }

# Tiers from config (llm.tiers), or built from the older llm.modes format (full -> full, game -> small).
function Get-TierConfig {
    if ($cfg.llm.tiers) { return @($cfg.llm.tiers) }
    $m = $cfg.llm.modes
    @(
        [pscustomobject]@{ name = 'full';  maxOthersMiB = 10240; whisper = $true;  model = $m.full },
        [pscustomobject]@{ name = 'small'; maxOthersMiB = 14848; whisper = $true;  model = $(if ($m.game) { $m.game } else { $m.full }) },
        [pscustomobject]@{ name = 'voice'; maxOthersMiB = 19456; whisper = $true;  model = $null },
        [pscustomobject]@{ name = 'none';  maxOthersMiB = $null; whisper = $false; model = $null }
    )
}
$Tiers = [ordered]@{}
foreach ($t in Get-TierConfig) {
    $file = $(if ($t.model) { Join-Path $paths.Models $t.model.file } else { $null })
    # first-guess footprint: weights + ~25% for context/compute buffers; replaced by the measured value once loaded
    $est = $(if ($file -and (Test-Path $file)) { [int]((Get-Item $file).Length / 1MB * 1.25) + 512 } else { 0 })
    $Tiers[$t.name] = @{ Model = $t.model; Max = $(if ($t.maxOthersMiB) { [int]$t.maxOthersMiB } else { [int]::MaxValue })
                         Whisper = [bool]$t.whisper -and [bool]$cfg.voice.enabled; EstLlm = $est }
}
$TierNames  = @($Tiers.Keys)
$EstWhisper = 1500
# Best tier allowed while a game/stream runs (llm.activityMaxTier, "" = no cap). Games size their texture pool from
# the VRAM free at launch, so dropping at once on game start gives them the room before they fill it.
$ActivityMax = $(if ($null -ne $cfg.llm.activityMaxTier) { "$($cfg.llm.activityMaxTier)" } else { 'small' })
if ($ActivityMax -and $ActivityMax -notin $TierNames) { $ActivityMax = '' }

function Get-TierArgs($tier) {
    $m = $Tiers[$tier].Model
    $a = @('-m', "`"$(Join-Path $paths.Models $m.file)`"",
           '--host', $(if ($Role -eq 'orchestrator') { '127.0.0.1' } else { '0.0.0.0' }), '--port', "$ServerPort", '--alias', $cfg.llm.alias) + @($cfg.llm.commonArgs)
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

# What the PC is doing, for the log and status only.
function Get-Activity($roots) {
    $svc = Get-Service $cfg.apollo.serviceName -ErrorAction SilentlyContinue
    if ((Test-Path $StreamFlag) -and ($null -eq $svc -or $svc.Status -eq 'Running')) { return 'stream' }
    $game = Find-RunningGame $roots
    if ($game) { return "game: $game" }
    'idle'
}

function Get-Override {
    if (-not (Test-Path $OverrideFile)) { return $null }
    $o = "$(Get-Content $OverrideFile -Raw)".Trim().ToLower()   # an empty file means "auto", not an error
    if ($o -in @('game', 'stream') -and $Tiers.Contains('small')) { return 'small' }   # older override names
    if ($Tiers.Contains($o)) { return $o }
    $null
}

# Total GPU memory in use, our own share (llama-server, Whisper) and the rest, in MiB; $null if unreadable.
function Get-Vram {
    try {
        $smi = (& nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits) | Select-Object -First 1
        $used, $total = $smi -split ',\s*' | ForEach-Object { [int]$_ }
        $ours = @{}
        Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $paths.Server } |
            ForEach-Object { $ours[$_.Id] = 'llm' }
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -match 'wyoming_faster_whisper' } | ForEach-Object { $ours[[int]$_.ProcessId] = 'whisper' }
        $llm = 0; $whisper = 0
        foreach ($s in (Get-Counter '\GPU Process Memory(*)\Dedicated Usage' -ErrorAction Stop).CounterSamples) {
            if ($s.InstanceName -match '^pid_(\d+)_') {
                $who = $ours[[int]$matches[1]]
                if ($who -eq 'llm') { $llm += $s.CookedValue } elseif ($who -eq 'whisper') { $whisper += $s.CookedValue }
            }
        }
        $llm = [int]($llm / 1MB); $whisper = [int]($whisper / 1MB)
        @{ Used = $used; Total = $total; Llm = $llm; Whisper = $whisper; Others = [math]::Max(0, $used - $llm - $whisper) }
    } catch { $null }
}

# Best tier the others' usage allows. A tier bigger than the current one also needs the margin and must fit.
function Get-AllowedTier($vram, $current) {
    $currentIdx = $TierNames.IndexOf($current)
    if ($currentIdx -lt 0) { $currentIdx = $TierNames.Count }
    for ($i = 0; $i -lt $TierNames.Count; $i++) {
        $t = $Tiers[$TierNames[$i]]
        if ($i -lt $currentIdx) {
            $need = $(if ($t.Model) { $t.EstLlm } else { 0 }) + $(if ($t.Whisper) { $EstWhisper } else { 0 })
            if ($vram.Others + $Margin -lt $t.Max -and $vram.Others + $need + $Headroom -le $vram.Total) { return $TierNames[$i] }
        }
        elseif ($vram.Others -lt $t.Max) { return $TierNames[$i] }
    }
    $TierNames[-1]
}

# Worker: what the orchestrator wants loaded. Keeps the last answer through short outages; after 60 s without an
# answer (orchestrator off or unreachable) nothing is wanted.
function Update-Assignment($tier, $activity) {
    try {
        $u = "$($cfg.cluster.orchestrator.TrimEnd('/'))/assignment?worker=$($cfg.cluster.name)&tier=$tier&activity=$([uri]::EscapeDataString($activity))"
        $a = Invoke-RestMethod -Uri $u -TimeoutSec 3
        if ([bool]$a.llm -ne [bool]$script:Assign.llm -or [bool]$a.whisper -ne [bool]$script:Assign.whisper) {
            Write-Log "orchestrator asks: llm $([bool]$a.llm), whisper $([bool]$a.whisper)"
        }
        $script:Assign = [pscustomobject]@{ llm = [bool]$a.llm; whisper = [bool]$a.whisper }; $script:AssignOkAt = Get-Date
    } catch {
        if (((Get-Date) - $script:AssignOkAt).TotalSeconds -ge 60 -and ($script:Assign.llm -or $script:Assign.whisper)) {
            Write-Log "orchestrator unreachable for 60 s: unloading"
            $script:Assign = [pscustomobject]@{ llm = $false; whisper = $false }
        }
    }
}

function Stop-Server {
    Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $paths.Server } | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        $_.WaitForExit(15000) | Out-Null
    }
}

function Start-Server($tier) {
    if (-not $Tiers[$tier].Model) { return $null }
    Write-Log "starting $tier ($($Tiers[$tier].Model.file))"
    Start-Process -FilePath $paths.Server -ArgumentList (Get-TierArgs $tier) -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $paths.Logs "server-$tier.out.log") `
        -RedirectStandardError  (Join-Path $paths.Logs "server-$tier.err.log")
}

function Test-SameServer($a, $b) {
    $ma = $Tiers[$a].Model; $mb = $Tiers[$b].Model
    $ma -and $mb -and ($ma.file -eq $mb.file) -and ((Get-TierArgs $a) -join ' ') -eq ((Get-TierArgs $b) -join ' ')
}

$roots = Get-GameRoots
if ($DryRun) {
    "tiers:"; foreach ($n in $TierNames) { $t = $Tiers[$n]
        "  {0,-6} others < {1,-6} model {2} (~{3} MiB) whisper {4}" -f $n, $(if ($t.Max -eq [int]::MaxValue) { '-' } else { $t.Max }),
            $(if ($t.Model) { $t.Model.file } else { '-' }), $t.EstLlm, $t.Whisper }
    $v = Get-Vram
    if ($v) { "VRAM: used $($v.Used) / $($v.Total) MiB; llama-server $($v.Llm), Whisper $($v.Whisper), others $($v.Others)"
              "tier now: $(Get-AllowedTier $v 'none')" } else { 'VRAM: unreadable (nvidia-smi / GPU counters)' }
    "activity: $(Get-Activity $roots)"; "game folders:"; $roots | ForEach-Object { "  $_" }
    return
}

Write-Log 'supervisor started (VRAM tiers)'
Write-Log "game folders: $($roots -join '; ')"
Stop-Server
$current = $null; $proc = $null; $since = Get-Date
$upgradeFrom = $null; $failures = 0; $rootsAge = Get-Date; $activity = 'idle'
$startAt    = $null   # delayed model start after a downgrade
$deferStart = $false  # model start postponed until the game/stream ends

while ($true) {
    try {
        if (((Get-Date) - $rootsAge).TotalMinutes -ge 30) { $roots = Get-GameRoots; $rootsAge = Get-Date }
        $newActivity = Get-Activity $roots
        if ($newActivity -ne $activity) { Write-Log "activity: $newActivity"; $activity = $newActivity }

        $vram = Get-Vram
        $override = Get-Override
        if ($override) { $desired = $override; $reason = 'manual override' }
        elseif ($null -eq $vram) { $desired = $(if ($current) { $current } else { $TierNames[0] }); $reason = 'VRAM unreadable, keeping tier' }
        else { $desired = Get-AllowedTier $vram $current; $reason = "others $($vram.Others) MiB ($activity)" }
        if (-not $override -and $ActivityMax -and $activity -ne 'idle' -and $TierNames.IndexOf($desired) -lt $TierNames.IndexOf($ActivityMax)) {
            $desired = $ActivityMax; $reason = "$activity started (capped at $ActivityMax)"
        }
        if ($IsWorker -and -not $override) {
            Update-Assignment $current $activity
            # no LLM unless asked: step down to the next tier without a model
            if (-not $Assign.llm -and $Tiers[$desired].Model) {
                $i = $TierNames.IndexOf($desired)
                while ($i -lt $TierNames.Count - 1 -and $Tiers[$TierNames[$i]].Model) { $i++ }
                $desired = $TierNames[$i]; $reason = "$reason; orchestrator doesn't need the LLM"
            }
        }

        # learn real footprints once a tier has been loaded for a minute
        if ($vram -and $current -and $Tiers[$current].Model -and $vram.Llm -gt 500 -and ((Get-Date) - $since).TotalSeconds -gt 60) {
            $Tiers[$current].EstLlm = $vram.Llm
        }
        if ($vram -and $vram.Whisper -gt 300) { $EstWhisper = $vram.Whisper }

        # Downgrade at once (the game needs the memory); upgrade only after it has been allowed for a while.
        $busy = $NoBusyLoads -and $activity -ne 'idle'
        $manual = $reason -eq 'manual override'
        $switch = $false
        if ($null -eq $current) { $switch = $true }
        elseif ($desired -ne $current -and $busy -and -not $manual -and $TierNames.IndexOf($desired) -lt $TierNames.IndexOf($current)) {
            $upgradeFrom = $null   # no upgrades (= model starts) while a game/stream runs
        }
        elseif ($desired -ne $current) {
            if ($TierNames.IndexOf($desired) -gt $TierNames.IndexOf($current) -or $reason -eq 'manual override') { $switch = $true }
            elseif (-not $upgradeFrom) { $upgradeFrom = Get-Date }
            elseif (((Get-Date) - $upgradeFrom).TotalSeconds -ge $UpDelay) { $switch = $true }
        }
        if ($desired -eq $current -or $switch) { $upgradeFrom = $null }

        if ($switch) {
            Write-Log "tier $current -> $desired ($reason)"
            $startAt = $null; $deferStart = $false
            $isDowngrade = $current -and $TierNames.IndexOf($desired) -gt $TierNames.IndexOf($current)
            if (-not ($current -and $proc -and -not $proc.HasExited -and (Test-SameServer $current $desired))) {
                Stop-Server; $proc = $null
                $hasModel = [bool]$Tiers[$desired].Model
                if ($hasModel -and -not $manual -and $busy) {
                    $deferStart = $true
                    Write-Log "$desired model loads after the $activity ends"
                } elseif ($hasModel -and -not $manual -and $isDowngrade -and $LoadDelay -gt 0) {
                    $startAt = (Get-Date).AddSeconds($LoadDelay)
                    Write-Log "unloaded; $desired model loads in $LoadDelay s"
                } else { $proc = Start-Server $desired }
            }
            $current = $desired; $since = Get-Date; $failures = 0
        }
        elseif ($deferStart -and -not $busy) {
            $deferStart = $false
            if ($Tiers[$current].Model) { Write-Log "activity idle: loading the $current model"; $proc = Start-Server $current }
        }
        elseif ($startAt -and (Get-Date) -ge $startAt) {
            $startAt = $null
            if ($busy) { $deferStart = $true; Write-Log "activity started: $current model waits until it ends" }
            elseif ($Tiers[$current].Model) { $proc = Start-Server $current }
        }
        elseif ($proc -and $proc.HasExited -and $busy) {
            Write-Log "server exited with code $($proc.ExitCode) during $activity; restarting after it ends"
            $proc = $null; $deferStart = $true
        }
        elseif ($proc -and $proc.HasExited) {
            $failures++
            Write-Log "server exited with code $($proc.ExitCode) (failure $failures), restarting"
            Start-Sleep -Seconds ([math]::Min(60, 5 * $failures))
            $proc = Start-Server $current
        }

        @{ mode = $current; tier = $current; model = $(if ($Tiers[$current].Model) { $Tiers[$current].Model.file } else { $null })
           reason = $reason; activity = $activity; since = $since.ToString('o'); gpu_released = -not ($Tiers[$current].Whisper -and (-not $IsWorker -or $Assign.whisper))
           vram_used = $(if ($vram) { $vram.Used } else { $null }); vram_others = $(if ($vram) { $vram.Others } else { $null })
           pending = $(if ($upgradeFrom) { $desired } else { $null }) } | ConvertTo-Json | Set-Content $StatusFile
    }
    catch { Write-Log "error: $_" }
    Start-Sleep -Seconds $(if ($activity -ne 'idle') { $BusyPoll } else { $PollSeconds })
}
