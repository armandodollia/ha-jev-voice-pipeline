# Sets up a worker PC for a multi-PC kit (config.json cluster.role = "worker"). Run elevated on the worker, from a
# folder that holds: config.json, scripts\, llama.cpp\build\bin\ (llama-server built for this GPU, plus the CUDA
# runtime DLLs), models\<the tier models>, voice\data\<Whisper model cache, optional>, requirements-stt.txt, and
# optionally python312\ (a copy of the orchestrator's base Python, used for the venv instead of an installed one).
# It creates the Whisper venv, lets only the orchestrator reach the LLM and Whisper ports, and registers the SYSTEM
# tasks HomeLLM (supervisor in worker mode) and HomeVoice (Whisper only). Re-running it is safe.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"
$cfg   = Read-KitConfig
$paths = Get-KitPaths
if ("$($cfg.cluster.role)" -ne 'worker') { throw 'config.json: cluster.role must be "worker"' }
if (-not (Test-Path $paths.Server)) { throw "Missing $($paths.Server)" }

# Whisper venv (same pinned packages as the orchestrator)
$venvPy = Join-Path $paths.Voice 'stt\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    # a bundled python312\ folder (copied from the orchestrator) wins, so both PCs run the same packages
    $bundled = Join-Path $paths.Root 'python312\python.exe'
    $base = $(if (Test-Path $bundled) { $bundled } else { $null })
    foreach ($v in $(if ($base) { @() } else { '3.12', '3.11', '3.10' })) {
        try { $p = & py "-$v" -c 'import sys; print(sys.executable)' 2>$null; if ($LASTEXITCODE -eq 0 -and $p) { $base = $p.Trim(); break } } catch { }
    }
    if (-not $base) { $base = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $base) { throw 'Python 3.10-3.12 not found (install it, or the py launcher)' }
    Write-Host "creating Whisper venv with $base"
    & $base -m venv (Join-Path $paths.Voice 'stt')
}
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $paths.Root 'requirements-stt.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

# Firewall: LLM and Whisper reachable from the orchestrator only
$orch = ([uri]$cfg.cluster.orchestrator).Host
Get-NetFirewallRule -DisplayName 'HomeLLM worker*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'HomeLLM worker (from orchestrator)' -Direction Inbound -Protocol TCP `
    -LocalPort $cfg.llm.port, $cfg.voice.ports.wyomingStt -RemoteAddress $orch -Action Allow | Out-Null
New-NetFirewallRule -DisplayName 'HomeLLM worker llama-server program' -Direction Inbound -Program $paths.Server `
    -RemoteAddress $orch -Action Allow | Out-Null
New-NetFirewallRule -DisplayName 'HomeLLM worker Whisper program' -Direction Inbound -Program $venvPy `
    -RemoteAddress $orch -Action Allow | Out-Null

# SYSTEM tasks at boot, restarted if they stop
$ps = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
foreach ($t in @(@{ Name = 'HomeLLM'; File = 'homellm.ps1' }, @{ Name = 'HomeVoice'; File = 'voice.ps1' })) {
    $a = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $paths.Scripts $t.File)`""
    Register-ScheduledTask -TaskName $t.Name -Action $a -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $set `
        -User SYSTEM -RunLevel Highest -Force | Out-Null
    Stop-ScheduledTask $t.Name -ErrorAction SilentlyContinue
    Start-ScheduledTask $t.Name
}
Write-Host "worker '$($cfg.cluster.name)' running; orchestrator $($cfg.cluster.orchestrator). Logs: $($paths.Logs)"
