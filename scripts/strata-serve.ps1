# Serves the big MoE model (README > Big MoE tier) on this kit's LLM port, for a tier with "external".
# Started by the "HomeLLM Strata" scheduled task (runs as the user who installed Strata/Basalt, because they live in
# that user's profile); homellm.ps1 starts the task when it picks the tier and stops it by killing this process tree.
#
# Two engines (README > Engine choice: Strata or Basalt):
#   strata  (default) Strata Terminal: works out of the box on Ada and other consumer cards.
#   basalt  (opt-in)  Basalt, Ada (sm_89) fork: faster decode and prompt reading, 256K context with direct parking.
#                     Build it yourself first (README > Basalt). Upstream Basalt is Blackwell-only.
# Pick one with config.json strata.engine, or override it here with $Engine ('strata' | 'basalt'); -Engine on the
# command line wins over both.
#
# Settings: config.json "strata": {
#   "engine": "strata" | "basalt",
#   "runConfig": path of Strata's strata-<model>.json, "strataCmd": optional path of strata.cmd
#                (default %LOCALAPPDATA%\Strata\terminal\current\bin\strata.cmd),
#   "basalt": { "root": the basalt_ada checkout (bin\basalt.exe), "config": basalt_home.json,
#               "cudaPath": CUDA 13 toolkit folder (its bin goes on PATH), "venvScripts": Basalt's Python venv Scripts
#               folder, "anyModelName": true (needs basalt/patches/0002: any model name reaches the one model) } }
# Environment variables in paths are expanded. Strata gets the port on its command line: cluster.localPorts.llm on an
# orchestrator (router.py owns the public port), else llm.port, bound to 127.0.0.1 on an orchestrator. Basalt reads
# host and port from its own config's "server" block: set them to the same values.
param([string]$Engine = '')

. "$PSScriptRoot\common.ps1"
$cfg   = Read-KitConfig
$paths = Get-KitPaths
$s     = $cfg.strata
if (-not $Engine) { $Engine = $(if ($s.engine) { "$($s.engine)" } else { 'strata' }) }
$orch  = "$($cfg.cluster.role)" -eq 'orchestrator'
$port  = $(if ($orch -and $cfg.cluster.localPorts.llm) { [int]$cfg.cluster.localPorts.llm } else { [int]$cfg.llm.port })
$hostIp = $(if ($orch) { '127.0.0.1' } else { '0.0.0.0' })
$log   = Join-Path $paths.Logs 'strata-serve.log'
New-Item -ItemType Directory -Force $paths.Logs | Out-Null
function X($p) { [Environment]::ExpandEnvironmentVariables("$p") }

if ($Engine -eq 'basalt') {
    $b = $s.basalt
    $root = X $b.root; $bcfg = X $b.config
    if (-not (Test-Path "$root\bin\basalt.exe")) { "$(Get-Date -Format s) basalt.exe not found under $root" | Out-File $log -Append -Encoding utf8; exit 1 }
    if (-not (Test-Path $bcfg)) { "$(Get-Date -Format s) strata.basalt.config not found: $bcfg" | Out-File $log -Append -Encoding utf8; exit 1 }
    $pre = @()
    if ($b.cudaPath) { $c = X $b.cudaPath; $pre += "$c\bin", "$c\bin\x64" }
    if ($b.venvScripts) { $pre += (X $b.venvScripts) }
    if ($pre) { $env:PATH = ($pre -join ';') + ";$env:PATH" }
    if ($b.anyModelName -ne $false) { $env:BASALT_ANY_MODEL_NAME = '1' }
    $env:PYTHONIOENCODING = 'utf-8'
    "$(Get-Date -Format s) starting basalt $bcfg (expects ${hostIp}:$port in its server block)" | Out-File $log -Append -Encoding utf8
    Set-Location $root
    # cmd's redirection keeps the log UTF-8 (Windows PowerShell 5.1's *>> writes UTF-16)
    cmd /c "bin\basalt.exe serve --config `"$bcfg`" >> `"$log`" 2>&1"
} else {
    $run = X $s.runConfig
    $cmd = $(if ($s.strataCmd) { X $s.strataCmd } else { "$env:LOCALAPPDATA\Strata\terminal\current\bin\strata.cmd" })
    if (-not (Test-Path $run)) { "$(Get-Date -Format s) strata.runConfig not found: $run" | Out-File $log -Append -Encoding utf8; exit 1 }
    "$(Get-Date -Format s) starting strata $run on ${hostIp}:$port" | Out-File $log -Append -Encoding utf8
    cmd /c "`"$cmd`" serve --profile `"$run`" --host $hostIp --port $port >> `"$log`" 2>&1"
}
"$(Get-Date -Format s) $Engine exited ($LASTEXITCODE)" | Out-File $log -Append -Encoding utf8
