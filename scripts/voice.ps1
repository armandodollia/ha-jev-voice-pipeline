# Voice services for Home Assistant and phone/watch apps, kept running and restarted if they exit:
#   whisper   Wyoming faster-whisper on the GPU (speech-to-text), biased toward HA entity names when a token is set
#   piper     Wyoming Piper on the CPU (text-to-speech)
#   speechApi OpenAI-style /v1/audio/transcriptions + /v1/audio/speech in front of both (for apps like Wristotle)
#   haRelay   runs /api/conversation/process requests through HA's Assist pipeline (localhost only)
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
$whisperArgs = @('-m', 'wyoming_faster_whisper', '--model', $v.whisperModel, '--language', $v.language,
                 '--device', 'cuda', '--compute-type', 'float16', '--beam-size', '5',
                 '--uri', "tcp://0.0.0.0:$($v.ports.wyomingStt)", '--data-dir', "`"$Data`"", '--download-dir', "`"$Data`"",
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

$procs = @{}; $fails = @{}
foreach ($n in $Services.Keys) { $procs[$n] = Start-Voice $n; $fails[$n] = 0 }
while ($true) {
    foreach ($n in $Services.Keys) {
        if ($procs[$n].HasExited) {
            $fails[$n]++
            Write-KitLog 'voice.log' "$n exited with code $($procs[$n].ExitCode) (failure $($fails[$n]))"
            Start-Sleep -Seconds ([math]::Min(60, 5 * $fails[$n]))
            $procs[$n] = Start-Voice $n
        }
    }
    Start-Sleep -Seconds 5
}
