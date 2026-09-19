# Socket Wizard — Prosper receptionist

Socket Wizard is a multilingual clinic receptionist built with Pipecat, LangGraph, Deepgram, Cartesia, and Vercel AI Gateway. English and Spanish use Cartesia stock voices; Catalan uses ElevenLabs. Jev provides a separate optional conversation assessment service.

V2 is the only application. The former `src/` runtime and dashboards have been removed. Scheduling rules, the Prosper client, audio codecs, language routing, and reusable design assets now live inside `v2/`.

## Start locally

```powershell
py -3 -m venv .venv-v2
.\.venv-v2\Scripts\python -m pip install -r requirements.txt
.\run.ps1
```

```bash
python3 -m venv .venv-v2
./.venv-v2/bin/python -m pip install -r requirements.txt
bash run.sh
```

The console is at `http://127.0.0.1:7861/`. It supports run history, transcript/action/error inspection, protected recordings, offline fixtures, natural text sessions, and microphone calls. The carrier endpoint is `/ws`, using Twilio Media Streams format without a Twilio account. Browser voice uses a short-lived, origin-bound ticket at `/ws/browser`.

Populate the untracked `v2/.env` from `v2/.env.example`. It overrides the root `.env`. Never commit credentials or recordings. Operator requests use `X-V2-Token`; tokens must not be placed in URLs.

## Execution modes

- `simulation`: synthetic clinic fixtures and local action receipts.
- `practice`: real Prosper clinic reads, but local action receipts.
- Live cutover remains gated while the provider-backed release is validated. See `docs/V2-STATUS.md` for current evidence and restrictions.

Paid voice and model experiments require `V2_ENABLE_PAID=true`. The offline scripted demo requires no provider credentials or spend. Text model duels still consume LLM credits.

The clinic is read-only: even Prosper submissions report actions the receptionist would make, rather than modifying appointments. HTTP acceptance, fixture results, Jev assessments, and official scoring are different signals.

## Verification

```powershell
.\.venv-v2\Scripts\python -m unittest discover -s v2/tests -v
.\.venv-v2\Scripts\python scripts/check_domain.py
.\.venv-v2\Scripts\python -m v2.evaluation --language es --database :memory:
.\.venv-v2\Scripts\python -m pip check
```

In `services/jev`, use Node 22.18 or newer and run `npm ci`, `npm run check`, and `npm test`.

## Project map

- `v2/api.py`, `v2/config.py`: server, access control, configuration.
- `v2/voice.py`, `v2/audio.py`, `v2/codecs.py`: Pipecat voice pipeline and recording.
- `v2/models.py`, `v2/workflow.py`: typed conversation state and LangGraph controller.
- `v2/clinic.py`, `v2/domain/`, `v2/platform_api/`: validated clinic operations.
- `v2/providers.py`: Vercel interpreter and Jev client.
- `v2/store.py`, `v2/evaluation.py`, `v2/simulator.py`: persistence, metrics, and evaluations.
- `v2/console.html`, `v2/web/`: operator interface and assets.
- `services/jev/`: authenticated TypeScript assessment function.

See [ARCHITECTURE.md](ARCHITECTURE.md), [RUNBOOK.md](RUNBOOK.md), and [PROSPER-TRACK.md](PROSPER-TRACK.md).
