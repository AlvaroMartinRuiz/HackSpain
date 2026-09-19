# Socket Wizard v2

- V2 is the only application. The user explicitly approved removal of the former `src/` runtime, old dashboards, and nine legacy scripts. Shared rules/client/codecs/language/design code now belongs to `v2/`; do not reintroduce imports from `src`.
- Entry point: `python -m v2` or the root `run.ps1` / `run.sh`. Use `.venv-v2/Scripts/python` on Windows and `.venv-v2/bin/python` on Linux. Install `requirements.txt`, which delegates to `v2/requirements.txt`.
- Keep the existing dependency pins; LangGraph's SDK requires websockets <16. Do not install into a legacy or unrelated environment.
- Root `.env` loads first and untracked `v2/.env` overrides it. Never read or print credential values, copy secrets into worktrees, or commit runtime databases/audio. Readiness should report names/presence only.

# Verification

- `.venv-v2/Scripts/python -m unittest discover -s v2/tests -v` runs provider-mocked/offline Python regressions, including actual LangGraph/Pipecat machinery with synthetic audio.
- `.venv-v2/Scripts/python scripts/check_domain.py` verifies deterministic scheduling/identity/date rules offline.
- `.venv-v2/Scripts/python -m v2.evaluation --language es --database :memory:` runs an offline synthetic two-intent fixture; `en` and `ca` are also supported.
- `.venv-v2/Scripts/python -m pip check` checks Python dependencies.
- In `services/jev`, run `npm ci`, `npm run check`, and `npm test` with Node >=22.18. If Node is absent, a local `nodejs-wheel==22.20.0` installation provides Node/npm without changing machine-wide configuration.
- `scripts/check_engine.py` and `scripts/smoke_test.py` are real clinic READ checks, not offline tests. `scripts/mock_call.py` uses configured voice providers and may spend credits. Do not run them under an offline-only label.
- A container build is `docker build -f v2/Dockerfile -t socket-wizard-v2 .`; a running Docker daemon is required. Runtime secrets/data must remain outside the image.

# Safety, completion, and metrics

- The model interprets caller turns; deterministic rules construct clinic action payloads. Keep independent intents per patient/request, current offer revisions, actual presentation and explicit confirmation boundaries.
- Interruptions invalidate obsolete LLM/TTS responses, not already-running actions. Preserve unresolved receipts and require reconciliation before replay.
- Completion requires all requests resolved and the final response actually sent before the quiet grace period. New caller speech revokes pending completion.
- Audio observations mean successful non-silent socket sends, not proof of caller playback. Both mu-law zero encodings count as silence. Unknown/unmeasured audio is not a passing measurement.
- Simulated receipts, HTTP acknowledgement, fixture grades and Jev model assessments are separate from official grading. Do not equate local completion or HTTP 200 with a scored pass.
- Provider diagnostics may contain exception type, phase, status, timings and model; never raw error bodies, auth headers, tokens or credential-bearing URLs.

# Providers and budget

- Voice uses Deepgram Nova-3, Cartesia stock voices for Spanish/English, and ElevenLabs for Catalan. Stock voices come first; voice cloning is deferred until consenting-owner recordings arrive.
- Vercel interpretation requires `AI_GATEWAY_API_KEY`. Cartesia requires `CARTESIA_API_KEY` and a verified `V2_CARTESIA_VOICE_ES`.
- Jev is a separate TypeScript service using fixed `typesafe-ai/jev` and rubric `conversation-v1`. Python uses `V2_JEV_URL`/`V2_JEV_TOKEN`; the service uses `JEV_SERVICE_TOKEN`, `JEV_ENABLE_PAID`, and gateway credentials.
- Paid execution requires explicit opt-in and one persistent project budget ledger. The user authorized checks within the existing $30 shared effort cap. Unknown charges stay reserved; this is not an account-wide provider cap. Do not reset, replace, or bypass the ledger.
- Parallel development does not authorize parallel paid tests. Only the coordinator runs paid probes, serially, and records mocked versus real-provider evidence distinctly.
- Current user scope is CODE DELIVERY ONLY: no deployment, endpoint switch, or real scored submission without separate approval. Implementing a provider client is not proof that its credentials or deployment work.

# Parallel collaboration

- Coordinator owns `feat/voice-v2`, shared API/config/auth/deployment wiring, domain-package migration, and `docs/V2-STATUS.md`.
- Five isolated worktrees/branches: `v2/devin-domain`, `v2/devin-voice`, `v2/devin-gateway`, `v2/devin-dashboard`, `v2/devin-evals`. Their launch revision is `b32caed`, whose runtime matches the earlier frozen code.
- Respect assigned path ownership and stable shared interfaces. Coordinator migrates old imports during integration; agents must not restore deleted legacy files.
- Commit and push meaningful tested checkpoints to each assigned branch; the coordinator integrates and pushes verified checkpoints to `feat/voice-v2`. Never push to `main`, force-push, rewrite history, or stage credentials/runtime data.
- Reports must identify exact tests, mocked versus provider-backed evidence, schema contracts, costs, and remaining blockers. Preserve test coverage when replacing legacy functionality.
