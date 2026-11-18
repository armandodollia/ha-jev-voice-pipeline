# Serves a Strata model (README > Strata tier) on this kit's LLM port, for a tier with "external".
# Started by the "HomeLLM Strata" scheduled task (runs as the user who installed Strata, because Strata lives in that
# user's profile); homellm.ps1 starts the task when it picks the tier and stops it by killing this process tree.
# Settings: config.json "strata": { "runConfig": path of Strata's strata-<model>.json, "strataCmd": optional path of
# strata.cmd (default %LOCALAPPDATA%\Strata\terminal\current\bin\strata.cmd) }. The port is cluster.localPorts.llm on
# an orchestrator (router.py owns the public port), else llm.port, bound to 127.0.0.1 on an orchestrator.
. "$PSScriptRoot\common.ps1"
$cfg   = Read-KitConfig
$paths = Get-KitPaths
$s     = $cfg.strata
$run   = [Environment]::ExpandEnvironmentVariables("$($s.runConfig)")
$cmd   = $(if ($s.strataCmd) { [Environment]::ExpandEnvironmentVariables("$($s.strataCmd)") } else { "$env:LOCALAPPDATA\Strata\terminal\current\bin\strata.cmd" })
$orch  = "$($cfg.cluster.role)" -eq 'orchestrator'
$port  = $(if ($orch -and $cfg.cluster.localPorts.llm) { [int]$cfg.cluster.localPorts.llm } else { [int]$cfg.llm.port })
$hostIp = $(if ($orch) { '127.0.0.1' } else { '0.0.0.0' })
$log   = Join-Path $paths.Logs 'strata-serve.log'
New-Item -ItemType Directory -Force $paths.Logs | Out-Null
if (-not (Test-Path $run)) { "$(Get-Date -Format s) strata.runConfig not found: $run" | Out-File $log -Append -Encoding utf8; exit 1 }
"$(Get-Date -Format s) starting $run on ${hostIp}:$port" | Out-File $log -Append -Encoding utf8
# cmd's redirection keeps the log UTF-8 (Windows PowerShell 5.1's *>> writes UTF-16)
cmd /c "`"$cmd`" serve --profile `"$run`" --host $hostIp --port $port >> `"$log`" 2>&1"
"$(Get-Date -Format s) exited ($LASTEXITCODE)" | Out-File $log -Append -Encoding utf8
