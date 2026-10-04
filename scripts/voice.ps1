# Voice services for Home Assistant and phone/watch apps, kept running and restarted if they exit:
#   whisper   Wyoming faster-whisper on the GPU (speech-to-text), biased toward HA entity names when a token is set
#   piper     Wyoming Piper on the CPU (text-to-speech)
#   speechApi OpenAI-style /v1/audio/transcriptions + /v1/audio/speech in front of both (for apps like Wristotle)
#   haRelay   runs /api/conversation/process requests through HA's Assist pipeline (localhost only)
# Whisper is stopped while the supervisor has released the GPU (gpu_released in state\status.json, its "none" tier)
# and started again when it isn't.
# Runs at boot as the SYSTEM scheduled task "HomeVoice" (see install.ps1).
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common.ps1"
$cfg   = Read-KitConfig
$paths = Get-KitPaths
$v     = $cfg.voice
$Data  = Join-Path $paths.Voice 'data'
New-Item -ItemType Directory -Force $Data, $paths.Logs | Out-Null

$sttPy = Join-Path $paths.Voice 'stt\Scripts\python.exe'
$ttsPy = Join-Path $paths.Voice 'tts\Scripts\python.exe'

# CTranslate2 only finds cuBLAS/cuDNN from the pip nvidia packages if their bin folders are on PATH.
$nv = Join-Path $paths.Voice 'stt\Lib\site-packages\nvidia'
if (Test-Path $nv) {
    $nvBins = (Get-ChildItem $nv -Directory | ForEach-Object { Join-Path $_.FullName 'bin' } | Where-Object { Test-Path $_ }) -join ';'
    $env:PATH = "$nvBins;$env:PATH"
}
$env:HF_HOME = Join-Path $Data 'hf'

$haUrl     = $cfg.homeAssistant.url.TrimEnd('/')
$tokenFile = Join-Path $paths.Root $cfg.homeAssistant.tokenFile
# Multi-PC: on the orchestrator Whisper listens on localhost:cluster.localPorts.stt and router.py takes the public port
# (the speech API keeps using the public port, so it is routed too). A worker runs Whisper only (voice.services).
$sttUri = $(if ("$($cfg.cluster.role)" -eq 'orchestrator' -and $cfg.cluster.localPorts.stt) { "tcp://127.0.0.1:$($cfg.cluster.localPorts.stt)" }
            else { "tcp://0.0.0.0:$($v.ports.wyomingStt)" })
$whisperArgs = @('-m', 'wyoming_faster_whisper', '--model', $v.whisperModel, '--language', $v.language,
                 '--device', 'cuda', '--compute-type', $(if ($v.computeType) { $v.computeType } else { 'int8_float16' }), '--beam-size', '5',
                 '--uri', $sttUri, '--data-dir', "`"$Data`"", '--download-dir', "`"$Data`"",
                 '--vad-clip', '--vad-filter')      # trim edges; drop non-speech so silence can't hallucinate text
if ($v.endpointingSec) { $whisperArgs += '--vad-endpointing', "$($v.endpointingSec)" }
if (Test-Path $tokenFile) {
    # Bias recognition toward HA's exposed entity/area/floor names. Token stays off the command line.
    $env:WYO_WHISPER_HASS_TOKEN_FILE = $tokenFile
    $whisperArgs += '--hass-api', "$haUrl/api", '--hass-refresh-seconds', '300'
}

$Services = [ordered]@{
    whisper   = @{ Exe = $sttPy; Args = $whisperArgs }
    piper     = @{ Exe = $ttsPy; Args = @('-m', 'wyoming_piper', '--voice', $v.piperVoice,
                   '--uri', "tcp://0.0.0.0:$($v.ports.wyomingTts)", '--data-dir', "`"$Data`"", '--download-dir', "`"$Data`"") }
    speechApi = @{ Exe = $sttPy; Args = @("`"$(Join-Path $paths.Scripts 'speech_api.py')`"", '--port', "$($v.ports.speechApi)",
                   '--stt-port', "$($v.ports.wyomingStt)", '--tts-port', "$($v.ports.wyomingTts)") }
    haRelay   = @{ Exe = $sttPy; Args = @("`"$(Join-Path $paths.Scripts 'ha_relay.py')`"", '--port', "$($v.ports.haRelay)",
                   '--ha', ($haUrl -replace '^http', 'ws')) }
}

if ($v.services) {   # e.g. ["whisper"] on a worker PC
    foreach ($n in @($Services.Keys)) { if ($n -notin @($v.services)) { $Services.Remove($n) } }
}

function Start-Voice($name) {
    Write-KitLog 'voice.log' "starting $name"
    $s = $Services[$name]
    Start-Process -FilePath $s.Exe -ArgumentList $s.Args -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $paths.Logs "$name.out.log") -RedirectStandardError (Join-Path $paths.Logs "$name.err.log")
}

# Clear leftovers from a previous run.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'wyoming_(faster_whisper|piper)|speech_api\.py|ha_relay\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# True while the supervisor has released the GPU to other programs.
$StatusFile = Join-Path $paths.State 'status.json'
function Test-GpuReleased {
    try { [bool](Get-Content $StatusFile -Raw | ConvertFrom-Json).gpu_released } catch { $false }
}
# True while a game/stream runs (status.json activity): a crashed Whisper is restarted only after it ends, because
# starting a CUDA model mid-game can trigger a GPU driver reset.
function Test-GameActive {
    try { "$((Get-Content $StatusFile -Raw | ConvertFrom-Json).activity)" -notin @('', 'idle') } catch { $false }
}

$procs = @{}; $fails = @{}
$paused = Test-GpuReleased
foreach ($n in $Services.Keys) {
    $fails[$n] = 0
    if ($n -eq 'whisper' -and $paused) { Write-KitLog 'voice.log' 'whisper paused (GPU released)'; $procs[$n] = $null; continue }
    $procs[$n] = Start-Voice $n
}
while ($true) {
    $released = Test-GpuReleased
    if ($released -and -not $paused) {
        Write-KitLog 'voice.log' 'whisper paused (GPU released)'
        if ($procs['whisper'] -and -not $procs['whisper'].HasExited) {
            Stop-Process -Id $procs['whisper'].Id -Force -ErrorAction SilentlyContinue
            $procs['whisper'].WaitForExit(15000) | Out-Null
        }
        $procs['whisper'] = $null; $paused = $true
    }
    elseif (-not $released -and $paused) {
        Write-KitLog 'voice.log' 'whisper resumed'
        $procs['whisper'] = Start-Voice 'whisper'; $fails['whisper'] = 0; $paused = $false
    }
    foreach ($n in $Services.Keys) {
        if (-not $procs[$n]) { continue }
        if ($procs[$n].HasExited -and $n -eq 'whisper' -and $cfg.llm.noLoadsDuringActivity -ne $false -and (Test-GameActive)) { continue }
        if ($procs[$n].HasExited) {
            $fails[$n]++
            Write-KitLog 'voice.log' "$n exited with code $($procs[$n].ExitCode) (failure $($fails[$n]))"
            Start-Sleep -Seconds ([math]::Min(60, 5 * $fails[$n]))
            $procs[$n] = Start-Voice $n
        }
    }
    Start-Sleep -Seconds $(if ("$($cfg.cluster.role)" -eq 'worker') { 2 } else { 5 })
}
