# Starts the agent's socket and the console on the same port. Host, port and
# reload all come from .env, so this file never disagrees with the console.
#   Console:  http://localhost:7860/
#   Endpoint: ws://localhost:7860/ws   (wss:// through ngrok)
Set-Location $PSScriptRoot

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "No hay .venv aqui. Mira el Readme."
    exit 1
}

& $python -m src.main
