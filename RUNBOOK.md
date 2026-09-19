# Socket Wizard v2 runbook

## Environment

Use a dedicated `.venv-v2` and the pinned `v2/requirements.txt`. The root requirements file delegates to it. Python 3.12 is used by the container; Python 3.14 is also used for local Windows verification.

```powershell
py -3 -m venv .venv-v2
.\.venv-v2\Scripts\python -m pip install -r requirements.txt
```

On Linux/macOS, use `python3 -m venv .venv-v2` and `.venv-v2/bin/python`.

Create an untracked `v2/.env` from `v2/.env.example`. Root `.env` values are loaded first; the v2 file overrides them. Keep keys out of source, terminal output, URLs, and screenshots.

Required for stock voice: `V2_OPERATOR_TOKEN`, `AI_GATEWAY_API_KEY`, `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`, `ELEVENLABS_API_KEY`, and a verified `V2_CARTESIA_VOICE_ES`. Enable paid requests deliberately with `V2_ENABLE_PAID=true`. English/Spanish use Cartesia; Catalan uses the configured ElevenLabs voice.

Practice clinic reads additionally need `PLATFORM_API_KEY`. Its base defaults to the documented Prosper API. The cached catalog remains in `data/catalog.json`.

## Start

```powershell
.\run.ps1
```

```bash
bash run.sh
```

Both run `python -m v2`. The default port is 7861. `GET /health` reports missing configuration without exposing values; configured keys alone do not prove provider acceptance.

Open `http://127.0.0.1:7861/`. Enter the operator token in the dashboard. Protected HTTP endpoints and the carrier `/ws` use `X-V2-Token`. Do not put long-lived tokens in query strings.

The current delivery does not authorize deployment or replacement of an existing remote endpoint. A carrier integration must send the configured authentication header and use mono 8 kHz mu-law Twilio messages.

## Offline checks

```powershell
.\.venv-v2\Scripts\python -m unittest discover -s v2/tests -v
.\.venv-v2\Scripts\python scripts/check_domain.py
.\.venv-v2\Scripts\python -m v2.evaluation --language en --database :memory:
.\.venv-v2\Scripts\python -m v2.evaluation --language es --database :memory:
.\.venv-v2\Scripts\python -m v2.evaluation --language ca --database :memory:
.\.venv-v2\Scripts\python -m pip check
```

In `services/jev`, use Node 22.18 or later: `npm ci`, `npm run check`, `npm test`.

`check_domain.py` is offline. `scripts/check_engine.py` and `scripts/smoke_test.py` contact the real clinic for reads. `scripts/fetch_catalog.py` refreshes the local catalog. `scripts/generate_assets.py --list` lists Quiver assets without making provider requests.

## Paid checks

`python -m v2.evaluation --duel --language es` runs a bounded model-to-model text conversation against fixtures. `--review` also sends the synthetic transcript to Jev. These require explicit paid opt-in and the persistent local budget ledger; neither submits to Prosper. Text-only is not equivalent to free when a model is used.

Configure the separate Jev function with `JEV_SERVICE_TOKEN`, `JEV_ENABLE_PAID`, and gateway credentials. Python uses `V2_JEV_URL` over HTTPS and the matching `V2_JEV_TOKEN`. Keep service deployment authorization separate from model-gateway access.

Run paid checks serially within the shared $30 effort cap. Do not reset the database or create fresh ledgers to bypass reservations. Check actual account usage separately; the local ledger does not measure teammates' activity.

`scripts/mock_call.py` sends carrier-format audio to v2 and may consume paid quota. It reads the operator token from configuration. Run it only against non-scored, explicitly approved test sessions.

## Container

```bash
docker build -f v2/Dockerfile -t socket-wizard-v2 .
```

The image contains only v2 and the clinic catalog. Supply credentials at runtime and mount persistent storage for `v2/.data`; never bake `.env`, recordings, or databases into the image. A running Docker daemon is required to verify the build.

## Diagnostics

Inspect the stored run report for caller turns, planned/presented responses, clinic reads, guard rejections, receipts, and provider errors. WAV availability and non-silent socket sends are distinct from verified playback. HTTP acceptance and local completion are not official scored passes.

A `401` means operator authentication failed; missing configuration is shown by `/health`. A failed or unknown action receipt must be reconciled, not blindly replayed. Preserve runtime evidence when debugging; do not delete databases or recordings to make metrics look clean.
