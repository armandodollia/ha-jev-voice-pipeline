<#
.SYNOPSIS
  Installs the Home LLM GPU kit: a local LLM for Home Assistant (llama.cpp Jev-mode fork, OpenAI API +
  /v1/decision) with automatic model switching for gaming/streaming, plus GPU Whisper / Piper voice
  services, an OpenAI-style speech API and an Assist-pipeline relay.

.DESCRIPTION
  Run from an elevated PowerShell in the kit folder. Settings live in config.json (created from
  config.example.json on first run). Every step is idempotent, so re-running is safe.

.EXAMPLE
  .\install.ps1 -CheckOnly          # check prerequisites and show the plan, change nothing
  .\install.ps1                     # full install
  .\install.ps1 -SkipVoice          # LLM only
  .\install.ps1 -Rebuild            # rebuild llama-server after changing the fork/branch
#>
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$SkipBuild,
    [switch]$Rebuild,
    [switch]$SkipModels,
    [switch]$SkipVoice,
    [switch]$SkipApollo,
    [switch]$SkipTailscale,
    [switch]$SkipTasks,
    [switch]$NoTokenPrompt
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar in PS 5.1
. "$PSScriptRoot\scripts\common.ps1"
$paths = Get-KitPaths

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    [ok] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    [!]  $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "    [x]  $msg" -ForegroundColor Red }

# ---------------------------------------------------------------- config
$cfgPath = Join-Path $paths.Root 'config.json'
if (-not (Test-Path $cfgPath)) {
    Copy-Item (Join-Path $paths.Root 'config.example.json') $cfgPath
    Write-Host "Created config.json from config.example.json. Review it (especially homeAssistant.url), then re-run." -ForegroundColor Yellow
    if (-not $CheckOnly) { return }
}
$cfg = Read-KitConfig

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $CheckOnly) { throw 'Run this from an elevated PowerShell (Run as administrator), or use -CheckOnly.' }

# ---------------------------------------------------------------- prerequisite discovery
function Find-VisualStudio {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) { return $null }
    $p = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($p -and (Test-Path "$p\VC\Auxiliary\Build\vcvars64.bat")) { $p } else { $null }
}

function Find-Cuda {
    $candidates = @()
    if ($env:CUDA_PATH) { $candidates += $env:CUDA_PATH }
    $root = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA'
    if (Test-Path $root) { $candidates += Get-ChildItem $root -Directory | Sort-Object Name -Descending | ForEach-Object FullName }
    foreach ($c in $candidates) { if (Test-Path "$c\bin\nvcc.exe") { return $c } }
    $null
}

function Test-PythonExe($exe) {
    try {
        $v = & $exe -c "import sys; print('%d.%d|%s' % (sys.version_info[0], sys.version_info[1], sys.executable))" 2>$null
        if ($v -match '^(\d+)\.(\d+)\|(.+)$') {
            $minor = [int]$Matches[2]
            if ([int]$Matches[1] -eq 3 -and $minor -ge 10 -and $minor -le 13) { return $Matches[3].Trim() }
        }
    } catch {}
    $null
}

function Find-Python {
    if ($cfg.voice.python) { return (Test-PythonExe $cfg.voice.python) }
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($ver in '3.12', '3.11', '3.13', '3.10') {
            $p = & py "-$ver" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $p) { $candidates += $p.Trim() }
        }
    }
    $pyenv = Join-Path $env:USERPROFILE '.pyenv\pyenv-win\versions'
    if (Test-Path $pyenv) {
        $candidates += Get-ChildItem $pyenv -Directory | Where-Object Name -match '^3\.1[0-3]\.' |
            Sort-Object { [version]$_.Name } -Descending | ForEach-Object { Join-Path $_.FullName 'python.exe' }
    }
    $candidates += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python31[0-3]\python.exe", 'C:\Program Files\Python31[0-3]\python.exe' -ErrorAction SilentlyContinue |
        ForEach-Object FullName
    foreach ($c in $candidates) { $ok = Test-PythonExe $c; if ($ok) { return $ok } }
    $null
}

function Get-LanAddress {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.PrefixOrigin -in 'Dhcp', 'Manual' -and $_.IPAddress -notlike '169.254.*' -and $_.InterfaceAlias -notmatch 'Tailscale|vEthernet|Loopback' } |
        Select-Object -First 1
}

# e.g. 192.168.0.20/24 -> 192.168.0.0/24 (for advertising the home network as a Tailscale subnet route)
function Get-LanCidr($addr) {
    $bytes = ([Net.IPAddress]::Parse($addr.IPAddress)).GetAddressBytes()
    $bits = $addr.PrefixLength
    for ($i = 0; $i -lt 4; $i++) {
        $keep = [math]::Max(0, [math]::Min(8, $bits - 8 * $i))
        $bytes[$i] = $bytes[$i] -band ((0xFF -shl (8 - $keep)) -band 0xFF)
    }
    "$(([Net.IPAddress]$bytes).ToString())/$bits"
}

Step 'Checking prerequisites'
$gpu = $null
try { $gpu = (& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null | Select-Object -First 1) } catch {}
if ($gpu) { Ok "NVIDIA GPU: $gpu" } else { Fail 'No NVIDIA GPU / driver found (nvidia-smi).' }

$vs   = Find-VisualStudio
$cuda = Find-Cuda
$py   = Find-Python
$lanAddr = Get-LanAddress
$lan  = if ($lanAddr) { $lanAddr.IPAddress } else { $null }
$ts   = Get-TailscaleExe
$useTailscale = $cfg.tailscale.enabled -and -not $SkipTailscale
$needBuild = -not $SkipBuild -and ($Rebuild -or -not (Test-Path $paths.Server))

if ($vs) { Ok "Visual Studio C++ tools: $vs" } elseif ($needBuild) { Fail 'Visual Studio 2022 with "Desktop development with C++" is required to build llama-server.' }
if ($cuda) { Ok "CUDA toolkit: $cuda" } elseif ($needBuild) { Fail 'CUDA toolkit (12.x) is required to build llama-server: https://developer.nvidia.com/cuda-downloads' }
if (Test-Path $paths.Server) { Ok "llama-server already built: $($paths.Server)" }
if ($cfg.voice.enabled -and -not $SkipVoice) {
    if ($py) { Ok "Python for voice services: $py" } else { Fail 'Python 3.10-3.13 not found (install 3.12, or set voice.python in config.json).' }
}
if ($lan) { Ok "LAN address: $lan" } else { Warn 'Could not determine the LAN IPv4 address.' }
if ($useTailscale) {
    if ($ts) {
        $tsState = Get-TailscaleStatus
        if ($tsState -and $tsState.BackendState -eq 'Running') { Ok "Tailscale: $($tsState.Self.DNSName.TrimEnd('.'))" }
        elseif ($cfg.tailscale.login) { Warn 'Tailscale installed but not logged in; the installer will start a login.' }
        else { Warn 'Tailscale not logged in and tailscale.login=false; tailnet access will be skipped.' }
    } else { Warn 'Tailscale not installed (winget install Tailscale.Tailscale); tailnet access will be skipped.' }
} else { Ok 'Tailscale disabled in config (LAN only)' }

$apolloConf = $cfg.apollo.confPath
$useApollo = -not $SkipApollo -and (($cfg.apollo.enabled -eq $true) -or ($cfg.apollo.enabled -eq 'auto' -and (Test-Path $apolloConf)))
if ($useApollo) { Ok "Apollo/Sunshine config: $apolloConf" } else { Warn 'Apollo/Sunshine not configured; stream mode must be triggered manually (state\override.txt).' }

if ($cfg.llm.tiers) { $tierModels = @($cfg.llm.tiers | Where-Object { $_.model } | ForEach-Object { $_.model }) }
else { $tierModels = @($cfg.llm.modes.PSObject.Properties | ForEach-Object { $_.Value }) }   # older config format
$modelFiles = @($tierModels | Sort-Object file -Unique)
Write-Host "`n    VRAM tiers:"
if ($cfg.llm.tiers) { foreach ($t in $cfg.llm.tiers) { Write-Host ("      {0,-6} others < {1,-6} {2}" -f $t.name, $(if ($t.maxOthersMiB) { $t.maxOthersMiB } else { '-' }), $(if ($t.model) { $t.model.file } else { '(no LLM)' })) } }
else { foreach ($p in $cfg.llm.modes.PSObject.Properties) { Write-Host ("      {0,-7} {1}" -f $p.Name, $p.Value.file) } }

if ($CheckOnly) { Write-Host "`nCheck only: nothing was changed." ; return }
if ($needBuild -and (-not $vs -or -not $cuda)) { throw 'Missing build prerequisites (see above).' }

New-Item -ItemType Directory -Force $paths.Models, $paths.State, $paths.Logs | Out-Null

# ---------------------------------------------------------------- build llama-server
if ($needBuild) {
    Step "Building llama-server ($($cfg.llm.fork.repo)@$($cfg.llm.fork.branch), CUDA arch $($cfg.llm.cudaArch))"
    $src = Join-Path $paths.Root 'llama.cpp'
    if ($Rebuild -and (Test-Path $src)) { Remove-Item $src -Recurse -Force }
    if (-not (Test-Path "$src\CMakeLists.txt")) {
        $zip = Join-Path $env:TEMP 'homellm-llama.cpp.zip'
        Invoke-WebRequest "https://github.com/$($cfg.llm.fork.repo)/archive/refs/heads/$($cfg.llm.fork.branch).zip" -OutFile $zip
        $tmp = Join-Path $env:TEMP 'homellm-llama.cpp'
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
        Expand-Archive $zip $tmp -Force
        Move-Item (Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName $src
        Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    $log = Join-Path $paths.Logs 'build.log'
    $cmd = "`"$vs\VC\Auxiliary\Build\vcvars64.bat`" && cd /d `"$src`" && " +
           "cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=$($cfg.llm.cudaArch) " +
           "-DGGML_NATIVE=OFF -DLLAMA_CURL=OFF -DCUDAToolkit_ROOT=`"$cuda`" && cmake --build build --config Release --target llama-server -j"
    Write-Host '    (this takes 15-30 minutes; output in logs\build.log)'
    cmd /c $cmd *> $log
    if (-not (Test-Path $paths.Server)) { Get-Content $log -Tail 30; throw 'Build failed; see logs\build.log' }
    Ok 'llama-server built'
}

# ---------------------------------------------------------------- models
if (-not $SkipModels) {
    Step 'Downloading models'
    foreach ($m in $modelFiles) {
        $dest = Join-Path $paths.Models $m.file
        $url  = "https://huggingface.co/$($m.repo)/resolve/main/$($m.file)"
        $expected = $null
        try { $expected = [int64](Invoke-WebRequest $url -Method Head -UseBasicParsing).Headers['Content-Length'] } catch {}
        if ((Test-Path $dest) -and $expected -and (Get-Item $dest).Length -eq $expected) { Ok "$($m.file) (already downloaded)"; continue }
        Write-Host "    $($m.file) ..."
        curl.exe -L -C - --retry 5 --fail -s -S -o $dest $url
        if ($LASTEXITCODE -ne 0) { throw "Download failed: $url" }
        Ok ("{0} ({1:N1} GB)" -f $m.file, ((Get-Item $dest).Length / 1GB))
    }
}

# ---------------------------------------------------------------- voice services
$voiceOn = $cfg.voice.enabled -and -not $SkipVoice
$basePy = $null
if ($voiceOn) {
    if (-not $py) { throw 'Python 3.10-3.13 is required for the voice services (or use -SkipVoice).' }
    Step 'Setting up voice services (Whisper on GPU, Piper, speech API, Assist relay)'
    New-Item -ItemType Directory -Force $paths.Voice | Out-Null
    $stt = Join-Path $paths.Voice 'stt'; $tts = Join-Path $paths.Voice 'tts'
    foreach ($venv in $stt, $tts) { if (-not (Test-Path "$venv\Scripts\python.exe")) { & $py -m venv $venv } }
    & "$stt\Scripts\python.exe" -m pip install -q --upgrade pip
    & "$stt\Scripts\python.exe" -m pip install -q "wyoming-faster-whisper[hass]" nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" websockets
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed for the speech-to-text environment' }
    & "$tts\Scripts\python.exe" -m pip install -q --upgrade pip
    & "$tts\Scripts\python.exe" -m pip install -q wyoming-piper
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed for the text-to-speech environment' }
    & "$tts\Scripts\python.exe" (Join-Path $paths.Scripts 'patch_piper.py')
    Ok 'Python environments ready'
    $basePy = (& "$stt\Scripts\python.exe" -c "import sys; print(sys._base_executable)").Trim()

    $tokenFile = Join-Path $paths.Root $cfg.homeAssistant.tokenFile
    if (-not (Test-Path $tokenFile) -and -not $NoTokenPrompt) {
        Write-Host "`n    Optional: a Home Assistant long-lived access token lets Whisper learn your device names."
        Write-Host '    (HA -> your profile -> Security -> Long-lived access tokens. A non-admin user is enough.)'
        $sec = Read-Host '    Paste token, or press Enter to skip' -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        if ($plain) {
            Set-Content -Path $tokenFile -Value $plain.Trim() -NoNewline
            $who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            icacls $tokenFile /inheritance:r /grant:r "${who}:(R,W)" '*S-1-5-18:(R)' '*S-1-5-32-544:(F)' | Out-Null
            try {
                Invoke-RestMethod "$($cfg.homeAssistant.url.TrimEnd('/'))/api/" -Headers @{ Authorization = "Bearer $($plain.Trim())" } -TimeoutSec 10 | Out-Null
                Ok 'Token saved and accepted by Home Assistant'
            } catch { Warn "Token saved, but Home Assistant at $($cfg.homeAssistant.url) did not accept it (check homeAssistant.url)." }
        }
    }
}

# ---------------------------------------------------------------- Tailscale: connect
$tsName = $null
if ($useTailscale -and $ts) {
    Step 'Connecting to Tailscale'
    $tsc = $cfg.tailscale
    $state = Get-TailscaleStatus
    if (-not ($state -and $state.BackendState -eq 'Running') -and $tsc.login) {
        $upArgs = @('up', '--timeout=10m')
        if ($tsc.unattended) { $upArgs += '--unattended' }
        if ($tsc.hostname)   { $upArgs += "--hostname=$($tsc.hostname)" }
        if ($tsc.authKeyFile) {
            $keyPath = if ([IO.Path]::IsPathRooted($tsc.authKeyFile)) { $tsc.authKeyFile } else { Join-Path $paths.Root $tsc.authKeyFile }
            $upArgs += "--auth-key=file:$keyPath"
        } else {
            Write-Host '    Open the login link Tailscale prints below and sign in (waits up to 10 minutes).'
        }
        & $ts @upArgs
        $state = Get-TailscaleStatus
    }
    if ($state -and $state.BackendState -eq 'Running') {
        # Keep the PC on the tailnet when nobody is signed in to Windows (after reboots, while streaming headless).
        $setArgs = @('set', "--unattended=$(([bool]$tsc.unattended).ToString().ToLower())")
        if ($tsc.hostname) { $setArgs += "--hostname=$($tsc.hostname)" }
        if ($tsc.advertiseLanRoute -and $lanAddr) {
            $cidr = Get-LanCidr $lanAddr
            $setArgs += "--advertise-routes=$cidr"
        }
        & $ts @setArgs
        $state = Get-TailscaleStatus
        $tsName = $state.Self.DNSName.TrimEnd('.')
        Ok "on the tailnet as $tsName ($($state.Self.TailscaleIPs -join ', '))"
        if ($tsc.advertiseLanRoute -and $cidr) {
            Warn "Advertising ${cidr}: approve it in the Tailscale admin console (Machines -> this PC -> Edit route settings)."
        }
    } else { Warn 'Tailscale is not connected; skipping tailnet access.' }
}

# ---------------------------------------------------------------- firewall
Step ('Configuring Windows Firewall (LAN' + $(if ($cfg.tailscale.enabled -and $cfg.tailscale.allowTailnetFirewall) { ' + tailnet' } else { '' }) + ' only)')
$remote = Get-KitRemoteAddresses $cfg
Get-NetFirewallRule -DisplayName 'HomeLLM llama-server', 'HomeLLM llama-server program', 'HomeVoice Wyoming', 'HomeVoice Python' -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'HomeLLM llama-server' -Direction Inbound -Protocol TCP -LocalPort $cfg.llm.port `
    -RemoteAddress $remote -Action Allow -Profile Any | Out-Null
# Windows auto-creates Block rules for a program when its firewall prompt goes unanswered (e.g. after starting a
# server by hand), and Block beats Allow: every client then silently fails to connect. An explicit Allow rule for
# the program stops the prompt from appearing; stale Block rules are removed.
Get-NetFirewallApplicationFilter | Where-Object { $_.Program -eq $paths.Server } | Get-NetFirewallRule |
    Where-Object Action -eq 'Block' | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'HomeLLM llama-server program' -Direction Inbound -Program $paths.Server -Action Allow `
    -Profile Any -RemoteAddress $remote | Out-Null
Ok "TCP $($cfg.llm.port) (LLM); llama-server allowed: $($paths.Server)"
if ($voiceOn) {
    $vp = $cfg.voice.ports
    New-NetFirewallRule -DisplayName 'HomeVoice Wyoming' -Direction Inbound -Protocol TCP `
        -LocalPort $vp.wyomingStt, $vp.wyomingTts, $vp.speechApi -RemoteAddress $remote -Action Allow -Profile Any | Out-Null
    # Windows auto-creates Block rules for a program when its firewall prompt goes unanswered, and Block
    # beats Allow, which silently breaks Home Assistant -> Whisper. An explicit Allow rule prevents the prompt.
    Get-NetFirewallApplicationFilter | Where-Object { $_.Program -eq $basePy } | Get-NetFirewallRule |
        Where-Object Action -eq 'Block' | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName 'HomeVoice Python' -Direction Inbound -Program $basePy -Action Allow `
        -Profile Any -RemoteAddress $remote | Out-Null
    Ok "TCP $($vp.wyomingStt), $($vp.wyomingTts), $($vp.speechApi) (voice); Python allowed: $basePy"
}

# ---------------------------------------------------------------- Apollo / Sunshine hook
if ($useApollo) {
    Step 'Adding stream start/stop hook to Apollo/Sunshine'
    $ps = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $paths.Scripts 'apollo-stream.ps1')`""
    $lines = @(Get-Content $apolloConf)
    # Keep any prep commands the user already has; replace only ours.
    $keep = @()
    $line = $lines | Where-Object { $_ -match '^\s*global_prep_cmd\s*=' } | Select-Object -First 1
    if ($line) {
        $parsed = $null
        try { $parsed = ConvertFrom-Json ($line -replace '^\s*global_prep_cmd\s*=\s*', '') } catch {}
        foreach ($e in $parsed) { if ("$($e.do)" -notmatch 'apollo-stream\.ps1') { $keep += $e } }   # foreach unrolls the array in PS 5.1 too
    }
    $all  = $keep + @([pscustomobject]@{ do = "$ps start"; undo = "$ps stop"; elevated = $false })
    Copy-Item $apolloConf "$apolloConf.homellm.bak" -Force
    $new = @($lines | Where-Object { $_ -notmatch '^\s*global_prep_cmd\s*=' }) + ('global_prep_cmd = ' + (ConvertTo-Json -Compress -InputObject $all))
    Set-Content -Path $apolloConf -Value $new
    Restart-Service $cfg.apollo.serviceName -ErrorAction SilentlyContinue
    Ok "Hook added (backup: $apolloConf.homellm.bak)"
}

# ---------------------------------------------------------------- scheduled tasks
if (-not $SkipTasks) {
    Step 'Registering startup tasks (run as SYSTEM at boot, restart on failure)'
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $tasks = [ordered]@{ HomeLLM = 'homellm.ps1' }
    if ($voiceOn) { $tasks.HomeVoice = 'voice.ps1' }
    foreach ($name in $tasks.Keys) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
            -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $paths.Scripts $tasks[$name])`""
        Register-ScheduledTask -TaskName $name -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) `
            -Settings $settings -Principal $principal -Force | Out-Null
        Start-ScheduledTask -TaskName $name
        Ok "$name started"
    }
}

# ---------------------------------------------------------------- tailnet HTTPS (for phone/watch apps)
# `tailscale serve` terminates HTTPS with a real certificate for <pc>.<tailnet>.ts.net and proxies to localhost.
# It is reachable from your tailnet only (that's `serve`; `funnel` would be the public internet, never used here).
$served = @()
if ($tsName) {
    $toServe = @(Get-KitServeMap $cfg | Where-Object { $voiceOn -or -not $_.Voice })
    if ($toServe.Count) {
        Step 'Publishing services over tailnet HTTPS (tailnet only, not the internet)'
        foreach ($s in $toServe) {
            & $ts serve --bg "--https=$($s.Https)" "http://127.0.0.1:$($s.Local)" | Out-Null
            if ($LASTEXITCODE -eq 0) { $served += $s; Ok ("{0,-13} {1}" -f $s.Name, (Format-TailnetUrl $tsName $s.Https '')) }
            else { Warn "tailscale serve failed for $($s.Name); enable HTTPS certificates in the Tailscale admin console (DNS page)." }
        }
    }
}

# ---------------------------------------------------------------- Home Assistant package
Step 'Writing Home Assistant package'
$tpl = Get-Content (Join-Path $paths.Root 'homeassistant\homellm.template.yaml') -Raw
$out = Join-Path $paths.Root 'homeassistant\homellm.yaml'
$hostForHa = if ($lan) { $lan } else { $env:COMPUTERNAME }
Set-Content -Path $out -Value ($tpl.Replace('__LLM_HOST__', $hostForHa).Replace('__LLM_PORT__', "$($cfg.llm.port)"))
Ok "$out  (copy to /config/packages/ on Home Assistant)"

# ---------------------------------------------------------------- verify
if (-not $SkipTasks) {
    Step 'Waiting for services'
    $deadline = (Get-Date).AddMinutes(5)
    $llmOk = $false
    while ((Get-Date) -lt $deadline -and -not $llmOk) {
        try { $llmOk = (Invoke-RestMethod "http://127.0.0.1:$($cfg.llm.port)/health" -TimeoutSec 2).status -eq 'ok' } catch { Start-Sleep 3 }
    }
    if ($llmOk) { Ok "LLM server healthy on port $($cfg.llm.port)" } else { Warn 'LLM server not healthy yet; check logs\supervisor.log and logs\server-*.err.log' }
    if ($voiceOn) {
        foreach ($p in $cfg.voice.ports.wyomingStt, $cfg.voice.ports.wyomingTts, $cfg.voice.ports.speechApi) {
            $up = $false
            while ((Get-Date) -lt $deadline -and -not $up) {
                $up = Test-NetConnection 127.0.0.1 -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue
                if (-not $up) { Start-Sleep 3 }
            }
            if ($up) { Ok "port $p listening" } else { Warn "port $p not up yet (first start downloads the Whisper model); see logs\voice.log" }
        }
    }
}

# ---------------------------------------------------------------- summary
$h = if ($lan) { $lan } else { '<this-pc>' }
Write-Host "`nDone. Settings for Home Assistant:" -ForegroundColor Cyan
Write-Host "  LLM (Local OpenAI LLM integration, server type llama.cpp): http://${h}:$($cfg.llm.port)/v1   model: $($cfg.llm.alias)"
if ($voiceOn) {
    Write-Host "  Wyoming Protocol speech-to-text: ${h}:$($cfg.voice.ports.wyomingStt)"
    Write-Host "  Wyoming Protocol text-to-speech: ${h}:$($cfg.voice.ports.wyomingTts)"
}
if ($served.Count) {
    Write-Host "`nOver your tailnet (phone / Pebble watch via Wristotle, see README):" -ForegroundColor Cyan
    foreach ($s in $served) {
        $path = if ($s.Name -eq 'Assist relay') { '' } else { '/v1' }
        Write-Host ("  {0,-13} {1}" -f $s.Name, (Format-TailnetUrl $tsName $s.Https $path))
    }
}
Write-Host "  Status: .\status.ps1    Uninstall: .\uninstall.ps1"
