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
