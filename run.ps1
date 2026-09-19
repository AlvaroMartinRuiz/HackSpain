# Starts the agent's socket and the console on the same port.
#   Console:  http://localhost:7860/
#   Endpoint: ws://localhost:7860/ws   (wss:// through ngrok)
Set-Location $PSScriptRoot
.\.venv\Scripts\uvicorn src.main:app --host 0.0.0.0 --port 7860 --reload
