# Called by Apollo/Sunshine's global prep command: "start" when a stream begins, "stop" when it ends.
param([ValidateSet('start', 'stop')][string]$Action)
$flag = Join-Path (Split-Path $PSScriptRoot -Parent) 'state\streaming.flag'
if ($Action -eq 'start') { New-Item -ItemType File -Force $flag | Out-Null }
else { Remove-Item $flag -Force -ErrorAction SilentlyContinue }
