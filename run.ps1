# Starts the v2 socket and console on the same port. Configuration loads from
# .env and v2/.env; automatic reload is disabled to protect active calls.
#   Console:  http://localhost:7861/
#   Endpoint: ws://localhost:7861/ws   (wss:// through ngrok)
Set-Location $PSScriptRoot

$python = ".\.venv-v2\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "No hay .venv-v2 aqui. RUNBOOK.md -> 'Desde cero en una maquina nueva'."
    exit 1
}

& $python -m v2
