<#
.SYNOPSIS
  Shows the current VRAM tier, service health, multi-PC routing (if cluster.role is set) and recent log lines.
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
$v = $cfg.voice.ports
if ("$($cfg.cluster.role)" -eq 'worker') {
    # a worker only runs the LLM and Whisper, and only while the orchestrator asks: idle is normal
    $wh = Test-NetConnection 127.0.0.1 -Port $v.wyomingStt -InformationLevel Quiet -WarningAction SilentlyContinue
    foreach ($p in @(@("LLM :$($cfg.llm.port)", $llm), @("Whisper :$($v.wyomingStt)", $wh))) {
        Write-Host ("{0,-22} {1}" -f $p[0], $(if ($p[1]) { 'up' } else { 'idle (not needed right now)' })) -ForegroundColor $(if ($p[1]) { 'Green' } else { 'Gray' })
    }
} else {
    Show "LLM :$($cfg.llm.port)" $llm
    if ($cfg.voice.enabled) {
        foreach ($p in @(@('Whisper', $v.wyomingStt), @('Piper', $v.wyomingTts), @('Speech API', $v.speechApi), @('Assist relay', $v.haRelay))) {
            Show "$($p[0]) :$($p[1])" (Test-NetConnection 127.0.0.1 -Port $p[1] -InformationLevel Quiet -WarningAction SilentlyContinue)
        }
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
# Multi-PC (README > Multiple PCs)
$role = "$($cfg.cluster.role)"
if ($role -eq 'orchestrator') {
    $port = $(if ($cfg.cluster.controlPort) { $cfg.cluster.controlPort } else { 8079 })
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$port/status" -TimeoutSec 3
        Write-Host "`nrouter: llm -> $($r.route.llm), whisper -> $($r.route.stt); workers asked to load: llm $($r.wanted.llm), whisper $($r.wanted.stt)"
        foreach ($w in $r.workers.PSObject.Properties) {
            Write-Host ("  worker {0,-10} last check-in {1} s ago, tier {2}, activity {3}; llm {4}, whisper {5}" -f $w.Name,
                $w.Value.last_poll_s, $w.Value.tier, $w.Value.activity,
                $(if ($r.backends.llm.($w.Name)) { 'up' } else { 'down' }), $(if ($r.backends.stt.($w.Name)) { 'up' } else { 'down' }))
        }
        if (-not $r.workers.PSObject.Properties.Count) { Write-Host '  no worker has checked in yet' -ForegroundColor Yellow }
    } catch { Write-Host "`nrouter: not answering on :$port" -ForegroundColor Red }
} elseif ($role -eq 'worker') {
    try {
        $a = Invoke-RestMethod "$($cfg.cluster.orchestrator.TrimEnd('/'))/status" -TimeoutSec 3
        Write-Host "`norchestrator $($cfg.cluster.orchestrator): activity $($a.activity); asks this worker for llm $($a.wanted.llm), whisper $($a.wanted.stt)"
    } catch { Write-Host "`norchestrator $($cfg.cluster.orchestrator): not reachable (this worker unloads after 60 s)" -ForegroundColor Yellow }
}
foreach ($t in 'HomeLLM', 'HomeVoice', 'HomeRouter') {
    $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($task) { Write-Host ("task {0,-17} {1}" -f $t, $task.State) }
}
$log = Join-Path $paths.Logs 'supervisor.log'
if (Test-Path $log) { Write-Host "`nrecent switches:"; Get-Content $log -Tail 5 | ForEach-Object { "  $_" } }
