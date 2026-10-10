<#
.SYNOPSIS
  Removes what install.ps1 set up: startup tasks, running servers, firewall rules, the Apollo/Sunshine hook
  and the tailnet HTTPS publishing. Leaves the kit folder (models, venvs, logs) so you can delete it yourself.
#>
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\scripts\common.ps1"
$paths = Get-KitPaths
$cfg = Read-KitConfig

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw 'Run this from an elevated PowerShell (Run as administrator).' }

foreach ($name in 'HomeLLM', 'HomeVoice', 'HomeRouter') {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "removed task $name"
    }
}

Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $paths.Server } | Stop-Process -Force
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'wyoming_(faster_whisper|piper)|speech_api\.py|ha_relay\.py|router\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host 'stopped servers'

Get-NetFirewallRule -DisplayName 'HomeLLM llama-server', 'HomeLLM llama-server program', 'HomeVoice Wyoming', 'HomeVoice Python',
    'HomeRouter control', 'HomeLLM worker*' -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule
Write-Host 'removed firewall rules'

$conf = $cfg.apollo.confPath
if (Test-Path $conf) {
    $lines = @(Get-Content $conf)
    $line = $lines | Where-Object { $_ -match '^\s*global_prep_cmd\s*=' } | Select-Object -First 1
    if ($line -and $line -match 'apollo-stream\.ps1') {
        $keep = @()
        foreach ($e in (ConvertFrom-Json ($line -replace '^\s*global_prep_cmd\s*=\s*', ''))) {
            if ("$($e.do)" -notmatch 'apollo-stream\.ps1') { $keep += $e }
        }
        $rest = @($lines | Where-Object { $_ -notmatch '^\s*global_prep_cmd\s*=' })
        if ($keep.Count) { $rest += 'global_prep_cmd = ' + (ConvertTo-Json -Compress -InputObject $keep) }
        Set-Content -Path $conf -Value $rest
        Restart-Service $cfg.apollo.serviceName -ErrorAction SilentlyContinue
        Write-Host 'removed Apollo/Sunshine hook'
    }
}

$ts = Get-TailscaleExe
if ($ts -and $cfg.tailscale.enabled) {
    foreach ($s in Get-KitServeMap $cfg) { & $ts serve "--https=$($s.Https)" off 2>$null | Out-Null }
    Write-Host 'stopped tailnet HTTPS publishing'
    if ($cfg.tailscale.advertiseLanRoute) {
        & $ts set --advertise-routes= 2>$null | Out-Null
        Write-Host 'stopped advertising the LAN route'
    }
    Write-Host 'Tailscale itself stays installed and logged in (tailscale logout / uninstall it separately if wanted).'
}
Remove-Item (Join-Path $paths.State 'streaming.flag') -ErrorAction SilentlyContinue
Write-Host "`nDone. Delete $($paths.Root) to remove models, environments and logs."
