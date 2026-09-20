#!/usr/bin/env bash
# run.ps1 for macOS and Linux. Same port, same .env, same everything.
#   Console:  http://localhost:7860/
#   Endpoint: ws://localhost:7860/ws   (wss:// through ngrok)
set -euo pipefail
cd "$(dirname "$0")"

python=./.venv/bin/python
if [ ! -x "$python" ]; then
  echo "No hay .venv aquí. Mira el Readme." >&2
  exit 1
fi

exec "$python" -m src.main
