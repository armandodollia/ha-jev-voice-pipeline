# Serves the big MoE model (README > Big MoE tier) on this kit's LLM port, for a tier with "external".
# Started by the "HomeLLM Strata" scheduled task (runs as the user who installed Strata/Basalt, because they live in
# that user's profile); homellm.ps1 starts the task when it picks the tier and stops it by killing this process tree.
#
# Engines (README > Choose an engine; one page each under docs\engines\):
#   strata  (default) Strata Terminal: works out of the box on RTX 20-50 and other consumer cards.
#   basalt  (opt-in)  Basalt: upstream (RTX 50 / Blackwell, sm_120) or the Ada fork basalt_ada (RTX 40, sm_89). Same
#                     launcher for both; only the checkout/archive folder and its config differ. Build or unpack it first.
#   hotrod  (remote)  Strata-HotRod: Linux only, so it is not started here. Run it on a Linux box (or WSL2, untested)
#                     and point a gateway model at it (docs\engines\hotrod.md). This script only logs that and exits 1.
# Pick one with config.json strata.engine, or pass -Engine ('strata' | 'basalt' | 'hotrod'); -Engine wins.
#
# Settings: config.json "strata": {
#   "engine": "strata" | "basalt" | "hotrod",
#   "runConfig": path of Strata's strata-<model>.json, "strataCmd": optional path of strata.cmd
#                (default %LOCALAPPDATA%\Strata\terminal\current\bin\strata.cmd),
#   "basalt": { "root": the Basalt or basalt_ada checkout / release folder, "config": basalt_home.json,
#               "exe": optional, relative to root or absolute (default bin\basalt.exe),
#               "args": optional extra arguments for `basalt serve` (array of strings, e.g. "--api-key-file", "<path>"),
#               "cudaPath": optional CUDA 13 toolkit folder (its bin goes on PATH), "venvScripts": optional Python venv
#               Scripts folder (a release archive's own .venv needs neither), "anyModelName": true (only with
#               basalt/patches/0001-any-model-name.patch; harmless without it) } }
# Environment variables in paths are expanded. Strata gets the port on its command line: cluster.localPorts.llm on an
# orchestrator (router.py owns the public port), else llm.port, bound to 127.0.0.1 on an orchestrator. Basalt reads
# host and port from its own config's "server" block: set them to the same values.
param([string]$Engine = '')

. "$PSScriptRoot\common.ps1"
$cfg   = Read-KitConfig
$paths = Get-KitPaths
$s     = $cfg.strata
if (-not $Engine) { $Engine = $(if ($s.engine) { "$($s.engine)" } else { 'strata' }) }
$Engine = $Engine.ToLowerInvariant()
$orch  = "$($cfg.cluster.role)" -eq 'orchestrator'
$port  = $(if ($orch -and $cfg.cluster.localPorts.llm) { [int]$cfg.cluster.localPorts.llm } else { [int]$cfg.llm.port })
$hostIp = $(if ($orch) { '127.0.0.1' } else { '0.0.0.0' })
$log   = Join-Path $paths.Logs 'strata-serve.log'
New-Item -ItemType Directory -Force $paths.Logs | Out-Null
function X($p) { [Environment]::ExpandEnvironmentVariables("$p") }
function Log($m) { "$(Get-Date -Format s) $m" | Out-File $log -Append -Encoding utf8 }

if ($Engine -notin 'strata', 'basalt', 'hotrod') { Log "unknown strata.engine '$Engine' (strata | basalt | hotrod)"; exit 1 }

if ($Engine -eq 'hotrod') {
    # Strata-HotRod is Linux-only and is reached over the network; the supervisor can't start, stop or unload it.
    Log ("strata.engine is hotrod: Strata-HotRod runs on another (Linux) machine and is not started here. Use a " +
         "gateway model with its url instead and drop the external tier from llm.tiers (docs\engines\hotrod.md)")
    exit 1
}

if ($Engine -eq 'basalt') {
    $b = $s.basalt
    $root = X $b.root; $bcfg = X $b.config
    $exe = $(if ($b.exe) { X $b.exe } else { 'bin\basalt.exe' })
    if (-not [IO.Path]::IsPathRooted($exe)) { $exe = Join-Path $root $exe }
    if (-not (Test-Path $exe)) { Log "basalt executable not found: $exe (strata.basalt.root / exe)"; exit 1 }
    if (-not (Test-Path $bcfg)) { Log "strata.basalt.config not found: $bcfg"; exit 1 }
    $pre = @()
    if ($b.cudaPath) { $c = X $b.cudaPath; $pre += "$c\bin", "$c\bin\x64" }
    if ($b.venvScripts) { $pre += (X $b.venvScripts) }
    if ($pre) { $env:PATH = ($pre -join ';') + ";$env:PATH" }
    if ($b.anyModelName -ne $false) { $env:BASALT_ANY_MODEL_NAME = '1' }
    $env:PYTHONIOENCODING = 'utf-8'
    $extra = (@($b.args) | Where-Object { "$_" -ne '' } | ForEach-Object { "`"$(X $_)`"" }) -join ' '
    Log "starting basalt $exe $bcfg $(if ($extra) { '(+ strata.basalt.args) ' })(expects ${hostIp}:$port in its server block)"
    Set-Location $root
    # cmd's redirection keeps the log UTF-8 (Windows PowerShell 5.1's *>> writes UTF-16). The line goes through a small
    # batch file because Windows PowerShell mangles a quoted executable path with spaces passed to cmd /c.
    New-Item -ItemType Directory -Force $paths.State | Out-Null
    $launcher = Join-Path $paths.State 'basalt-launch.cmd'
    "@`"$exe`" serve --config `"$bcfg`" $extra >> `"$log`" 2>&1" | Out-File $launcher -Encoding default
    cmd /c "`"$launcher`""
} else {
    $run = X $s.runConfig
    $cmd = $(if ($s.strataCmd) { X $s.strataCmd } else { "$env:LOCALAPPDATA\Strata\terminal\current\bin\strata.cmd" })
    if (-not (Test-Path $run)) { Log "strata.runConfig not found: $run"; exit 1 }
    Log "starting strata $run on ${hostIp}:$port"
    cmd /c "`"$cmd`" serve --profile `"$run`" --host $hostIp --port $port >> `"$log`" 2>&1"
}
Log "$Engine exited ($LASTEXITCODE)"
