<#
.SYNOPSIS
  Shows the current VRAM tier, service health and recent log lines.
  .\status.ps1 -Mode full|small|voice|none|auto   pins a tier (auto = back to automatic VRAM tiers).
  Pin a tier while long GPU jobs need the LLM to stay put (e.g. a teacher model for training data).
#>
param([string]$Mode)
. "$PSScriptRoot\scripts\common.ps1"
$paths = Get-KitPaths
$cfg = Read-KitConfig

if ($Mode) {
    $override = Join-Path $paths.State 'override.txt'
    if ($Mode -eq 'auto') { Remove-Item $override -ErrorAction SilentlyContinue } else { Set-Content $override $Mode }
    Write-Host "tier set to $Mode (takes effect within ~5 s)"
}

$statusFile = Join-Path $paths.State 'status.json'
if (Test-Path $statusFile) {
    $s = Get-Content $statusFile -Raw | ConvertFrom-Json
    Write-Host ("tier:   {0}  ({1})`nmodel:  {2}`nsince:  {3}`nVRAM:   {4} MiB used, {5} MiB by other programs; activity: {6}" -f `
        $s.tier, $s.reason, $s.model, $s.since, $s.vram_used, $s.vram_others, $s.activity)
    if ($s.pending) { Write-Host "pending: switching to $($s.pending) after the upgrade delay" }
} else { Write-Host 'supervisor has not written a status yet' }

function Show($name, $ok) { Write-Host ("{0,-22} {1}" -f $name, $(if ($ok) { 'up' } else { 'DOWN' })) -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' }) }
$llm = try { (Invoke-RestMethod "http://127.0.0.1:$($cfg.llm.port)/health" -TimeoutSec 3).status -eq 'ok' } catch { $false }
Write-Host ''
Show "LLM :$($cfg.llm.port)" $llm
if ($cfg.voice.enabled) {
    $v = $cfg.voice.ports
    foreach ($p in @(@('Whisper', $v.wyomingStt), @('Piper', $v.wyomingTts), @('Speech API', $v.speechApi), @('Assist relay', $v.haRelay))) {
        Show "$($p[0]) :$($p[1])" (Test-NetConnection 127.0.0.1 -Port $p[1] -InformationLevel Quiet -WarningAction SilentlyContinue)
    }
}
if ($cfg.tailscale.enabled) {
    $st = Get-TailscaleStatus
    if ($st -and $st.BackendState -eq 'Running') {
        $name = $st.Self.DNSName.TrimEnd('.')
        Write-Host "`ntailnet: $name ($($st.Self.TailscaleIPs -join ', '))"
        foreach ($s in Get-KitServeMap $cfg) {
            $path = if ($s.Name -eq 'Assist relay') { '' } else { '/v1' }
            Write-Host ("  {0,-13} {1}" -f $s.Name, (Format-TailnetUrl $name $s.Https $path))
        }
    } else { Write-Host "`ntailnet: not connected" -ForegroundColor Yellow }
}
foreach ($t in 'HomeLLM', 'HomeVoice') {
    $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($task) { Write-Host ("task {0,-17} {1}" -f $t, $task.State) }
}
$log = Join-Path $paths.Logs 'supervisor.log'
if (Test-Path $log) { Write-Host "`nrecent switches:"; Get-Content $log -Tail 5 | ForEach-Object { "  $_" } }
