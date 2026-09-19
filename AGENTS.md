# Acoustic verification

- `scripts/test_noise.py` is the offline regression suite for `scripts/check_noise.py`. Run it with the project virtualenv Python. It uses generated temporary WAVs and fake transcribers; it must not call voice providers, the clinic, or the LLM.
- `scripts/check_noise.py --help` describes the acoustic harness. Without `--live`, it only validates/calibrates local WAV fixtures. Offline rows are `not_run`, not passing recognition tests.
- Live acoustic trials spend configured streaming-STT quota. They use the production `CallSession` recognition/barge-in callbacks with simulated ongoing agent speech, but disable conversation handling and submissions. They do not test playback, Twilio transport, appointment outcomes, or official scoring.
- Supply clean test/consented speech with its exact transcript and separate noise WAVs. The official street/television/room/car recordings are not bundled. Generated tones in unit tests are not substitutes for those recordings.
- Reports include fixture hashes and measured pre-mu-law calibration levels. Peak limiting may change the effective SNR. WER retains accents and does not expand spoken digits. Transcripts are excluded unless explicitly requested; do not commit reports containing patient data.
- `src.obs.store` initializes its global database at import time. Use `DB_PATH=:memory:` for isolated checks rather than the running dashboard database. The noise probe guards its session import and records trial events in memory.
- The existing `scripts/check_fixes.py` uses FastAPI `TestClient` with the application lifespan. Despite its no-voice-minutes description, startup can warm the real TTS cache. Set `TTS_WARM_CACHE=false` in the test process before running that suite; do not change a running server's configuration to run tests. Its clinic checks still use read-only network requests.

# Submission and audio metrics

- Run `.venv/Scripts/python scripts/test_call_metrics.py` and `node --test scripts/test_dashboard_metrics.cjs` for the offline backend and dashboard metric regressions. They use no paid providers or clinic calls.
- `missing_submission_calls` counts recently completed in-memory calls with no recorded submission attempt. HTTP acceptance is not an official scored pass.
- `silent_calls` counts completed voice calls with finalized outbound telemetry and no non-silent mu-law frame successfully sent. Both zero encodings (`0xff`, `0x7f`) count as digital silence. This measures socket output, not intelligibility or confirmed caller playback.
- `audio_output` events are cumulative snapshots at startup, first successful frame, first non-silent frame, and finalization. Do not sum their counters or infer sent audio from a TTS response, a transcript, or silence-padded recordings.
- Old or incomplete measurements remain `unknown`; text rehearsals are `not_applicable`. New audio telemetry requires the updated server process; do not restart an active call to deploy it.

# Failure diagnostics and call completion

- Run `.venv/Scripts/python scripts/test_llm_diagnostics.py` and `.venv/Scripts/python scripts/test_call_flow.py` for provider-mocked/offline checks. No live voice or LLM quota is needed. The latter includes a two-intent voice-pipeline fixture through clean closure.
- LLM failures record exception type, request/stream phase, HTTP status, elapsed/first-output time, model and tool round. Never put raw provider error bodies, request headers, tokens or URLs into diagnostic events.
- The dashboards display HTTP acknowledgement separately from the official scored outcome, which is not imported. Dry-run records are excluded from the HTTP-accepted KPI. A local `completed` call status is not a scored pass.
- Completion is explicit: `finish_call` requires all requests resolved, a successful/latest submission, no queued caller input, and no unresolved tool failure. Closure waits for non-silent confirmation output to finish and then 15 seconds of quiet. New caller input or further tool work revokes the request. Text rehearsals do not auto-close.
- STT final callbacks enqueue work rather than waiting for the LLM. Finalization drains that queue within the existing recovery budget. Interruptions cancel only the LLM/TTS response, not an already-running tool/submission; later actions from the obsolete response must not execute.
- Audio generations must be checked again after a pacing sleep, before sending a frame. Silence handling must not stay disabled indefinitely after a VAD event with no committed speech.

# Isolated v2 laboratory

- v2 lives in `v2/` and `services/jev/`; it does not replace `src.main` or the current phone endpoint. It reuses the deterministic domain library, not the old call orchestrator.
- Use `.venv-v2/Scripts/python` on Windows (`.venv-v2/bin/python` on Linux). Install from `v2/requirements.txt` into that separate environment. LangGraph's SDK requires websockets <16, while v1 pins 17; do not install v2 dependencies into `.venv`.
- Start the lab with `.venv-v2/Scripts/python -m v2`; default address is `127.0.0.1:7861`. `V2_OPERATOR_TOKEN` defaults to the existing `CONSOLE_TOKEN`. Tokens are sent in `X-V2-Token`, never query strings. `/health` reports missing configuration without exposing values.
- Run `.venv-v2/Scripts/python -m unittest discover -s v2/tests -v`. These tests include actual LangGraph/Pipecat machinery with synthetic audio and mocked providers. In `services/jev`, run `npm ci`, `npm run check`, and `npm test`.
- `.venv-v2/Scripts/python -m v2.evaluation --language es` runs an offline two-intent fixture and persists its trace in `v2/.data/runs.db`. `en` and `ca` are also supported. `--database :memory:` is useful for offline smoke checks.
- `--duel` runs a bounded two-model text conversation using the configured Vercel model; `--review` evaluates the new synthetic transcript with Jev. Both require `V2_ENABLE_PAID=true`, appropriate credentials, and the persistent project budget ledger. Neither can submit to Prosper. The caller sees the goal and agent's replies, not backend IDs or expected grading records.
- Paid voice also requires explicit opt-in and all providers/voice IDs. Vercel needs `AI_GATEWAY_API_KEY`; Cartesia needs `CARTESIA_API_KEY` and a verified Spanish stock voice in `V2_CARTESIA_VOICE_ES`. Catalan uses the ElevenLabs fallback. Cloning is not provisioned until the consenting owner's recordings arrive.
- The Jev service is a separate TypeScript/Vercel function, with `JEV_SERVICE_TOKEN` and `JEV_ENABLE_PAID`. Python uses `V2_JEV_URL` and the matching `V2_JEV_TOKEN`. Its model is fixed to `typesafe-ai/jev`, its rubric to `conversation-v1`, and its API uses AI SDK 7.0.105's experimental evaluation interface. This isolated dependency is newer than the preferred seven-day release age because older SDKs do not expose the integration.
- The $30 ledger stores conservative per-request reservations and only releases them when actual costs are reconciled. Unknown outcomes remain reserved. It is not a provider-enforced or shared-account-wide spending cap; teammates' unrelated requests are not visible to it. Do not run concurrent paid batches or reset the ledger to bypass it.
- The current API allows only simulation and read-only-clinic practice. Live cutover is intentionally locked even if `V2_ALLOW_SUBMISSIONS` is set; carrier acceptance, clinic-rule parity, actual language/voice validation, and explicit release approval are still required.
- Build a container from the repository root with `docker build -f v2/Dockerfile -t socket-wizard-v2 .`. The Dockerfile-specific ignore file excludes secrets, old recordings, environments, and runtime data. A running Docker daemon is required; provider keys must be injected at runtime, not baked into the image.
- Run records include a code/catalog fingerprint, configuration manifest, typed action receipts, and distinct fixture/model/official grade provenance. Audio observations mean successful socket sends, not proof of caller playback. SQLite/local WAV storage is the local bootstrap; production Postgres/object storage and automatic improvement promotion are separate future gates.
