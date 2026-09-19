# V2 parallel delivery plan

## Active scope override

The user subsequently requested v2 as the sole application and explicitly confirmed deletion of the v1 runtime, old dashboards, obsolete scripts, and `docs/PLAN_ARREGLOS.md`. The coordinator migrates shared scheduling/client/audio/language/design code into v2 before deletion. Earlier instructions below to preserve `src/` or the v1 endpoint are historical and superseded for code delivery. Runtime credentials, databases, recordings, and existing remote deployments remain untouched. Five isolated agent branches launch from `b32caed`; assignments are in `V2-STATUS.md`. Paid checks are authorized within the existing shared $30 cap, run serially by the coordinator. Deployment and endpoint switching are not authorized.

## Goal and baseline

Ship a functional, observable receptionist with stock voices first. Voice cloning is the final enhancement, not a dependency. This plan can be handed unchanged to teammates or coding agents.

- Integration branch: `feat/voice-v2`.
- Frozen code baseline: `72df603` (initial v2 laboratory).
- Baseline verification: 33 v2 Python tests, 4 Jev tests, TypeScript checks, and `pip check` passed. These are offline/provider-mocked checks, not live-provider certification.
- The baseline contains the prior noise-harness branch. `origin/main` has newer v1 model/voice/dashboard work that is not automatically mixed into this baseline. Reused domain modules were unchanged when this plan was prepared. The coordinator reviews upstream changes before integration.
- Existing v1 service and port 7860 stay untouched. V2 defaults to port 7861 and currently rejects live-cutover mode.
- The current frontend is a laboratory. Real conversational parity, real voice quality, deployment and release validation remain work to do.

Status and checkpoint history are in [V2-STATUS.md](V2-STATUS.md). No lane is automatically assigned and no subagents have been launched by publishing this plan.

## Ownership matrix

Only the designated lane edits its paths. All other files are read-only unless the coordinator approves a boundary change.

| Lane | Planned branch | Exclusive write ownership | Main deliverable |
| --- | --- | --- | --- |
| A — Conversation/domain | `v2/domain` | `v2/models.py`, `v2/workflow.py`, `v2/clinic.py`, new `v2/domain/`, `v2/tests/test_workflow.py`, new `v2/tests/test_domain_*.py` | Complete, safe conversational workflows |
| B — Realtime voice | `v2/voice` | `v2/voice.py`, `v2/audio.py`, `v2/tests/test_voice.py`, new `v2/tests/test_audio_*.py` | Actual stock-voice calls in all three languages |
| C — Gateway/Jev | `v2/gateway` | `v2/providers.py`, `services/jev/`, new `v2/tests/test_provider_*.py` and `v2/tests/test_jev_*.py` | Verified model gateway and deployed evaluation adapter |
| D — Operator UI | `v2/dashboard` | `v2/console.html`, new `v2/web/`, new `v2/tests/ui/` | Reused operator dashboard wired to real v2 data |
| E — Evals/data | `v2/evals` | `v2/store.py`, `v2/evaluation.py`, `v2/simulator.py`, new `v2/fixtures/`, `v2/tests/test_evaluation.py`, new `v2/tests/test_store_*.py` | Reproducible evaluation results and bounded data capture |
| F — Integration/release | `feat/voice-v2` | `v2/api.py`, `v2/config.py`, `v2/__main__.py`, `v2/requirements.txt`, `v2/Dockerfile*`, `v2/.env.example`, `v2/tests/test_api_providers.py`, new `v2/tests/test_integration_*.py`, shared docs and `AGENTS.md` | API/auth, contract coordination, acceptance and deployable product |

Lane F is the coordinator role, not an extra agent editing the same files as everyone else. `src/`, existing v1 scripts, the current deployment, root dependency pins and other people's untracked files are read-only for all lanes. Reuse the domain library as an import; request a reviewed adapter/change if an upstream limitation appears.

## Dependency order

```text
F0: freeze launch revision, ownership, shared contracts and budget rules
         |
         +-- A0: propose additive domain/schema needs -- F review --> A1
         +-- B0: transport/codec/turn tests with mocked providers --> B1
         +-- C0: gateway/rubric/auth checks with mocks -----------> C1
         +-- D0: reuse UI against current endpoints/mock DTOs ---> D1
         +-- E0: fixtures, metric definitions, storage tests ----> E1

A + C + F -> functional text receptionist
B + C + F -> provider-backed stock voice smoke
A + B + C + F -> real multi-intent voice acceptance
D + E + F -> usable dashboard, recordings, reproducible eval reports
all lanes -> release candidate -> explicit team approval -> cutover
voice cloning -> only after the functional release is validated
```

A0 is a small schema/contract checkpoint, not completion of all domain work. B, C, D and E can start immediately against the existing contracts. Do not block the audio pipeline on Jev judging: Jev begins as a post-call assessment service, not the voice-generating model.

## Shared contract rules

1. Lane A owns `Operation`, `TurnDecision`, `CallState`, `Intent`, `Offer` and `Reply`. Keep changes additive and backwards compatible. Publish proposed fields/defaults and a fixture before consumers depend on them. Incompatible changes require a schema-version decision by F.
2. Current controller entry points are `turn(text, decision=None, expected_epoch=None)`, `interrupt(source=...)` and `presented(reply, interrupted=False)`. F/B must not bypass presentation or revision checks to make a demo pass.
3. `Reply` currently carries `response_id`, `text`, `language`, `epoch`, `offers` and `completion`. B owns actual audio handling, not business intent state.
4. C's interpretation adapter returns `TurnDecision`; it does not submit actions. Its context projection must be updated in coordination with A when A adds fields. Do not serialize entire private patient charts into the model prompt.
5. E owns store/report schemas and action/budget persistence. Reports distinguish `platform_receipt`, `simulation`, fixture grades, model assessments and the nullable official grade. HTTP 200 is not a scored pass. Unknown/unmeasured values are not zero or success.
6. F owns server configuration, routing, authentication and admission. Neither caller inputs nor model outputs may select live mode, a provider URL, credentials, or a higher spending limit.
7. Public interfaces must have a contract test. Shared-file changes go through their owner, not through concurrent edits by another lane.

### Existing operator HTTP contract

- `GET /health`: public readiness; no credential values.
- `GET /api/budget`: authenticated local reservation ledger.
- `GET /api/demo?language=en|es|ca`: authenticated synthetic fixture.
- `POST /api/rehearse`: authenticated fixture execution; always simulated actions.
- `GET /api/runs/{run_id}`: authenticated report and metrics.
- `GET /api/runs/{run_id}/audio/inbound|outbound`: authenticated WAV access.
- `/ws`: carrier-compatible WebSocket, currently authenticated by `X-V2-Token`.

### Proposed additions — not implemented in the baseline

F and E must agree a bounded `GET /api/runs` listing before D integrates it. Suggested response: `{runs: [summary], next_cursor: null|string}`, with summaries containing run/call IDs, mode, language, creation time, lifecycle status, metrics and explicit grade provenance. D may use a clearly marked mock DTO until that endpoint lands. Start with polling; a streaming feed is not required to ship the first product.

If the reused browser-talk UI is included, F must provide a browser-compatible authentication contract first. Browser WebSockets cannot set arbitrary `X-V2-Token` headers. Prefer an authenticated HTTP request that issues a short-lived, single-use ticket, followed by a WebSocket subprotocol handshake. Validate expiry, replay and Origin, and never log/echo the ticket. Do not put the long-lived operator token in a URL or bypass WebSocket authentication. Keep browser test sessions server-bound to non-scored mode.

## Ready-to-delegate task briefs

### A — Conversation and clinic-rule parity

**Mission:** Turn the lab into a receptionist that completes real requests, rather than just passing the scripted two-intent fixture.

**Work:**
- Audit the current six outcome paths: book, cancel, reschedule, register, no-action and escalation.
- Publish A0 schema needs for carried constraints, identity/confirmation evidence, alternate insurance and missing-field questions. Coordinate these with C's model-context projection.
- Implement natural collection/correction of required fields without repeatedly asking answered questions.
- Preserve patient identity, offer revision and actual presentation boundaries. A changed patient/date/provider invalidates old confirmation.
- Cover distinct patient intents, two cancellations, corrections, relative dates, empty windows, insurance/referral rules and urgent escalation.
- Make refusals explain the actual validated rule. Do not silently substitute another date/site or treat an unsupported path as a successful booking.
- Use clock/catalog/clinic fixtures for deterministic tests. No case-specific scored-answer shortcuts.

**Acceptance:** Positive and negative tests for each outcome; no invented IDs, stale-offer writes, unconfirmed actions or wrong-patient cross-contamination; multi-intent completion only after all requests resolve. Mandatory English/Spanish/Catalan text cases. Identify any parity gaps explicitly instead of claiming full support.

**Handoff:** Schema/fixture checkpoint first, then green workflow increments; list any required C/F follow-ups. Run the workflow suite and the existing deterministic domain checks.

### B — Pipecat voice and stock TTS

**Mission:** Make an actual call work, using stock voices. Do not work on voice cloning.

**Work:**
- Validate Twilio-shaped `connected/start/media/stop`, original call SID preservation, mono 8 kHz mu-law and paced 20 ms output.
- Keep Twilio REST hangup disabled. Prosper's `clear` is ineffective; stopping local generation/queues is mandatory.
- Validate English/Spanish Cartesia voices and the Catalan fallback. Do not quietly substitute Spanish for Catalan.
- Test VAD/STT turn boundaries, one-word final confirmations, interrupted synthesis, stale queued output and new speech during completion grace.
- Measure successful socket sends, not just synthesized bytes. A partially played/interrupted offer is not fully presented.
- Fix provider setup/shutdown, timeout and cleanup behavior without cancelling an already-running action.
- Add an audio-loop fixture that reaches the real pipeline without making paid requests.

**Acceptance:** Offline transport/codec/cancellation tests pass; then coordinator-run short real-provider probes pass in each language, followed by an actual multi-intent call. Save local artifacts and summarized latency/error results. Report unsupported provider/model behavior explicitly.

**Handoff:** Stock voice IDs and language/model configuration requests to F; caller transcript and safe artifact references to E. Never include keys or raw private recordings in the PR.

### C — Vercel models and Jev

**Mission:** Verify the generative model route and the separate typed evaluation route. Jev is not the conversation generator.

**Work:**
- Verify the current Vercel text-model request and structured decision validation; improve diagnostics for authentication, rate limits, timeouts and invalid responses without exposing raw provider bodies/secrets.
- Keep the context projection aligned with A's schemas and current offers; bound inputs/output/retries.
- Verify `typesafe-ai/jev` through the pinned AI SDK evaluation API, fixed question pack and authenticated adapter.
- Validate probabilities and run/question-pack correlation. Assessments must remain distinct from fixture/official outcomes.
- Prepare deployment for the supplied Vercel team (`socket-ec48`). Team ID/gateway access are not themselves Vercel deployment credentials; ask F for a verified project/login if necessary.
- Run paid smoke calls only through the coordinator's budget window. Do not independently benchmark a fleet of models.

**Acceptance:** Mocked auth/error/shape tests and TypeScript checks pass; a bounded real text-model call and real Jev call succeed through verified routes; the deployed adapter rejects unauthorized, oversized and invalid requests. Publish the actual deployment URL only after verification.

**Handoff:** Required env-variable names, verified model IDs/limits, adapter endpoint, question-pack version and sanitized request/usage evidence. Never key values.

### D — Reuse the operator dashboard

**Mission:** Adapt the existing dashboard rather than redesigning the product from scratch.

**Work:**
- Read the latest v1 UI from `origin/main`, including its talk/audio features, as reference. Copy/adapt into owned v2 paths; do not edit the working v1 UI.
- Show readiness, run history, transcript, audio, action receipts, errors and stage timings from actual v2 reports.
- Keep missing submissions, silent audio and unknown/unmeasured audio distinct. Keep simulated receipts, HTTP acceptance, fixture grades and model assessments distinct.
- Display reserved/estimated cost separately from reconciled actual cost.
- Use the current authenticated endpoints first, then the agreed run-list contract. No fabricated live status or passing scores.
- Fetch protected recordings with authenticated requests and play blob URLs; do not leak tokens through audio URLs. Preserve playback across polling updates and revoke unused blob URLs.
- If adding microphone calls, wait for F's ticket contract. Reuse the talk UI only after protocol/auth adaptation and label paid test calls clearly.

**Acceptance:** Runs the three-language fixture from the UI, displays real stored results, handles 401/missing configuration/empty history, escapes transcript content, and contains no provider credentials. Browser checks at narrow and desktop widths; no frontend redesign or new framework required to pass.

**Handoff:** UI screenshots/console results, endpoint contract gaps and browser-testing instructions. The existing lab remains usable while the richer UI is integrated.

### E — Evaluation, artifacts and feedback reports

**Mission:** Make experiments reproducible and failures explainable before attempting automatic improvement.

**Work:**
- Add deterministic fixtures covering A's supported outcomes and B's audio/interrupt cases.
- Keep caller goals separate from grading oracles. Caller agents see their persona and observed replies, not expected backend IDs/results or tool traces.
- Extend text duels and then audio duels with turn/time limits, fixed scenario/clock/noise versions, error attribution and bounded concurrency.
- Persist model/config/code/rubric provenance, transcripts, actual audio availability, receipts and metric summaries.
- Define stable metrics for task completion, all-intents completion, entity errors, interruptions, meaningful-response latency, provider failures and cost.
- Produce a failure-review report: evidence, likely category, affected cases, and suggested next experiment. Do not auto-edit code, deploy changes, relax clinic rules or equate model judgments with ground truth.
- Maintain one persistent action/budget ledger. Never sum cumulative snapshots, erase unresolved actions, reset spending, or silently migrate/delete existing run data.

**Acceptance:** Existing fixture grades stay correct; invalid/provider-error runs are distinguishable from semantic failures; all three languages are represented; simulated calls cannot submit real actions; baseline/candidate reports retain holdout provenance and unknown fields. No paid run until F reserves its budget.

**Handoff:** Machine-readable local run summaries plus small sanitized repo summaries, reproducible commands and any API listing/storage contract for F/D. Raw audio, patient traces and runtime DB files stay out of Git.

### F — Shared API, deployment and integration

**Mission:** Keep the integration branch coherent and convert the pieces into a usable release candidate.

**Work:**
- Confirm lane owners and launch revision. Review A/E contract changes before dependent implementations diverge.
- Own configuration/authentication, API routes, optional browser tickets, dependency locks and container packaging.
- Keep lab/practice credentials and action sinks isolated; live cutover remains disabled until explicit release approval.
- Adapt current readiness so absent voice-clone recordings do not block a stock-voice product. Account for required stock voices and actual provider readiness rather than just key presence.
- Coordinate deployment credentials and stock-voice selection without putting secrets into source, task prompts or logs.
- Merge small lane checkpoints, run cross-lane tests, and push the tested integration result immediately.
- Run paid smoke/integration tests serially against one authoritative ledger, tagging results with commit and configuration.

**Acceptance:** All offline suites pass together; functional stock-voice conversation verified in each language; six outcomes and multi-intent/correction cases exercised; old app remains available; operator can inspect the resulting evidence; no credential leak or unapproved production submission. Test the Docker build when its daemon is available and verify hosted networking before announcing deployment.

**Handoff:** Release checklist, known limitations, actual artifact/deployment references, rollback route, and a request for explicit team cutover approval. Voice cloning comes afterward.

## Repository collaboration protocol

### Claim and isolate

- F is the single editor of the status board and shared ownership table.
- Claim a lane with F before editing. Use a separate clone or worktree per worker; never have multiple agents checking out branches in the same directory.
- After the planning checkpoint is pushed, F records the exact launch SHA. Every lane branches from that SHA, not a moving `main` or whatever branch happens to be checked out.
- Use a unique local port per worktree and preserve the existing service on 7860.

Example, after replacing `<LAUNCH_SHA>` with F's recorded revision:

```bash
git fetch origin
git worktree add -b v2/domain ../HackSpain-v2-domain <LAUNCH_SHA>
```

A separate directory is deliberate. Each worktree needs its own dependency environment; inject secrets privately and never copy them into tracked files.

### Intermediate pushes

Push after each meaningful code/test checkpoint: first reproduction or contract test, a working increment, and the verified handoff. A clearly labeled failing regression may be pushed to a draft lane branch; it must not be merged into the integration branch while failing.

```bash
git status --short
git diff --check
git add <explicit-owned-files>
git commit -m "v2/A: describe the tested change"
git push -u origin v2/domain
```

Do not stage the whole worktree blindly, force-push, rewrite shared history, push to `main`, or create empty commits after read-only commands. If only test results change, report them in the draft PR rather than inventing a code change.

Open one draft PR per lane with **base `feat/voice-v2`**, not `main`. Put dependency requests and progress comments there; F records summaries in the status board. Only F merges the integration branch. Workers merge an explicitly announced integration checkpoint into their own branch when needed; no unilateral rebase or upstream merge.

Checkpoint/PR template:

```text
Lane / owner:
Branch / base SHA / checkpoint SHA:
What changed:
Tests run and results:
Mocked versus actual-provider evidence:
Spent / reserved / unknown cost:
Contract changes requested:
Blockers / dependencies:
Next checkpoint:
```

### Budget and secrets

- Total authorized API budget for this effort remains $30, shared with teammates' account activity. This plan authorizes no additional spend.
- Parallel coding and offline tests are allowed. Paid provider checks are serialized by F; all other lanes request a test window instead of spending independently.
- A suggested planning envelope is $2 for gateway/Jev checks, $8 for stock voice, $4 for conversational checks, $6 for evaluations, and $10 integration reserve. These are planning ceilings within the same $30, not separate grants or balances.
- Worktree-local SQLite files do not coordinate spending across machines. Paid tests must use F's single persistent ledger and agreed provider/account limits. Reservations are not invoice evidence, and unrelated teammates' spending cannot be inferred from the local ledger.
- Credentials are supplied through local environment/provider secret stores. Never put keys, tokens, private recordings or raw provider error bodies into this board, PRs, commits or agent prompts. Rotate chat-exposed keys before production use.

## First functional release: definition of done

1. Stock-voice caller can complete a booking and a two-intent conversation through the real chosen transport.
2. Book, reschedule, cancel, register, justified no-action and escalation have passing positive/negative cases.
3. Spanish, English and Catalan have actual-provider speech checks, not only translated templates.
4. Corrections and interruptions cannot produce stale or unconfirmed actions; in-flight receipts are not lost.
5. Operator can inspect transcript/audio availability, actions, errors, latency and budget from the dashboard.
6. Simulations remain isolated, real HTTP receipts are not called scored passes, and unknown measurements remain unknown.
7. Full offline regression runs are green; live probes and their costs are recorded separately.
8. Explicit team approval is obtained before replacing the current endpoint or enabling live submissions.

Not required for this first release: cloned voices, a new frontend framework, broad model tournaments, automatic production self-modification, or a distributed data-platform migration.
