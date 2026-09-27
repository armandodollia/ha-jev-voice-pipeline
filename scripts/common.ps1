# Shared helpers for the Home LLM GPU kit. Dot-source this file: . "$PSScriptRoot\common.ps1"
# Compatible with Windows PowerShell 5.1 (used by the scheduled tasks) and PowerShell 7.

$KitRoot = Split-Path $PSScriptRoot -Parent

function Read-KitConfig {
    $path = Join-Path $KitRoot 'config.json'
    if (-not (Test-Path $path)) { throw "Missing $path. Run install.ps1 (or copy config.example.json to config.json)." }
    Get-Content $path -Raw | ConvertFrom-Json
}

function Get-KitPaths {
    [pscustomobject]@{
        Root    = $KitRoot
        Scripts = Join-Path $KitRoot 'scripts'
        Server  = Join-Path $KitRoot 'llama.cpp\build\bin\llama-server.exe'
        Models  = Join-Path $KitRoot 'models'
        Voice   = Join-Path $KitRoot 'voice'
        State   = Join-Path $KitRoot 'state'
        Logs    = Join-Path $KitRoot 'logs'
    }
}

# Quote a JSON value so llama-server receives it intact through CreateProcess
# (e.g. {"enable_thinking":false} -> "{\"enable_thinking\":false}").
function ConvertTo-NativeJsonArg($obj) {
    $json = $obj | ConvertTo-Json -Compress -Depth 5
    '"' + $json.Replace('"', '\"') + '"'
}

function Write-KitLog($file, $msg) {
    $paths = Get-KitPaths
    New-Item -ItemType Directory -Force $paths.Logs | Out-Null
    Add-Content -Path (Join-Path $paths.Logs $file) -Value ('{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $msg)
}

# Remote addresses allowed through the firewall: the local subnet, plus the Tailscale ranges if enabled.
function Get-KitRemoteAddresses($cfg) {
    $addr = @('LocalSubnet')
    if ($cfg.tailscale.enabled -and $cfg.tailscale.allowTailnetFirewall) { $addr += '100.64.0.0/10', 'fd7a:115c:a1e0::/48' }
    $addr
}

function Get-TailscaleExe {
    $exe = 'C:\Program Files\Tailscale\tailscale.exe'
    if (Test-Path $exe) { $exe } else { $null }
}

# Returns the parsed `tailscale status --json`, or $null if Tailscale isn't installed/running.
function Get-TailscaleStatus {
    $ts = Get-TailscaleExe
    if (-not $ts) { return $null }
    try { & $ts status --json 2>$null | ConvertFrom-Json } catch { $null }
}

# The tailnet services the kit publishes over HTTPS: name, https port, local target port.
function Get-KitServeMap($cfg) {
    $m = @()
    if ($cfg.tailscale.serve.speechApi) { $m += [pscustomobject]@{ Name = 'Speech API';   Https = $cfg.tailscale.serve.speechApi; Local = $cfg.voice.ports.speechApi; Voice = $true } }
    if ($cfg.tailscale.serve.haRelay)   { $m += [pscustomobject]@{ Name = 'Assist relay'; Https = $cfg.tailscale.serve.haRelay;   Local = $cfg.voice.ports.haRelay;   Voice = $true } }
    if ($cfg.tailscale.serve.llm)       { $m += [pscustomobject]@{ Name = 'LLM API';      Https = $cfg.tailscale.serve.llm;       Local = $cfg.llm.port;              Voice = $false } }
    $m
}

function Format-TailnetUrl($dnsName, $httpsPort, $path) {
    $portPart = if ("$httpsPort" -eq '443') { '' } else { ":$httpsPort" }
    "https://$dnsName$portPart$path"
}
