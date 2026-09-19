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
- `.venv-v2/Scripts/node.exe --test v2/tests/ui/core.test.mjs v2/tests/ui/voice.test.mjs v2/tests/ui/structure.test.mjs` runs offline UI/audio helpers and structure checks on Windows.
- `.venv-v2/Scripts/python -m v2.tests.ui.browser_smoke --browser "C:/Program Files/Google/Chrome/Application/chrome.exe"` runs a real headless-browser smoke with ephemeral local servers, a fake operator token, in-memory databases, and a fake microphone. It checks authentication, three language fixtures, history, paid gating, desktop/mobile overflow, browser WebSocket capture/playback, mute/unmute, hangup and microphone release on logout. The voice server uses mocked providers; no real provider requests or deployment occur.
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
- Browser patient calls use one Call button, automatic language routing, and no per-session paid checkbox. `V2_PUBLIC_BROWSER_CALLS=true` enables calling without entering or exposing an operator token. Private history, recordings, text tools and carrier access remain operator-authenticated. Automatic sessions use the configured default language for the greeting, then follow the caller's detected language.
- Public browser tickets accept an empty body, require an allowed Origin, and always select read-only clinic practice. They are single-use and expire normally, with one public call at a time, six ticket requests per client address per minute, bounded pending tickets and bounded rate state. Keep server-side paid opt-in and the existing persistent budget ledger; never open private APIs to make the caller page token-free.
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

# Local tunnel preparation

- Optional local tunneling uses the official `ngrok==1.7.0` Python SDK, installed into `.venv-v2` with `pip install --only-binary=:all: --no-deps ngrok==1.7.0`; this does not change the application dependency pins. Run `pip check` afterward.
- The SDK does not load dotenv files itself. Import `v2.config` before using `ngrok.forward(..., authtoken_from_env=True)` so the ignored V2 override supplies `NGROK_AUTHTOKEN`. Forward to `Config().port` and keep the tunnel process alive. Do not print the token or raw ngrok exceptions.
- An existing local ngrok agent can be inspected at `http://127.0.0.1:4040/api/tunnels`; report only public URLs and upstream host/port, not captured requests or headers. `ERR_NGROK_334` means the requested endpoint is already online. Do not stop or repoint an existing tunnel or backend without explicit approval.
- Browser API requests intentionally omit cookies. Include the non-secret `ngrok-skip-browser-warning: 1` header on same-origin API requests; otherwise ngrok can return its HTML warning with HTTP 200 instead of JSON. Python/curl checks can miss this browser-only failure. The headless-browser smoke emulates this interstitial when the header is absent.
