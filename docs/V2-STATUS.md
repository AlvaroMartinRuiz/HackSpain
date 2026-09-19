# V2 delivery status

The status board on `feat/voice-v2` is authoritative. Copies on task branches are snapshots. Only the coordinator edits this board.

## Current state

- V2 is the sole application. The user explicitly approved v1 deletion; shared rules, client, codecs, language helpers and design assets were migrated before removal.
- Five isolated implementation lanes launched from `b32caed` and are integrated. Background agents could edit but their shell commands were denied, so the coordinator ran verification, commits and pushes.
- Delivery scope is CODE ONLY. No remote deployment, endpoint switch or scored submission was performed.
- Stock voice pipeline: Pipecat + Deepgram Nova-3 + Cartesia English/Spanish + ElevenLabs Catalan. LangGraph controls typed, independent request state. Vercel provides interpretation; Jev is a separate optional assessment adapter.
- Operator dashboard supports history, transcripts, receipts, errors/timings, protected audio, offline fixtures, natural text sessions and origin-bound, single-use-ticket browser voice.
- Simulation/practice remain the defaults. Live carrier operation requires both explicit submission and release approval. Browser/text rehearsal never inherits the live action sink.
- The user asked to stop expanding unit tests and prioritize delivery. Final verification is compilation, all 18 offline outcome/language fixtures, and a real headless Chrome operator smoke at desktop/mobile widths. All passed without paid API calls.
- Earlier verification checkpoints passed 181 assembled Python tests, 41 UI tests, 15 Jev tests, TypeScript checking, deterministic domain checks and `pip check`. That full test count predates the final small integration corrections; it is not a claim that the entire expanded suite was rerun afterward.
- The real Prosper health, authenticated health, directory and clinic READ checks passed (4/4). No patient records were printed and no clinic action was submitted.
- Paid API spend from this effort is $0. The existing shared effort allowance remains $30; unrelated teammates' usage is not measured here.

## Integrated lanes

| Lane | Branch | Lane checkpoint | Evidence |
| --- | --- | --- | --- |
| Conversation/domain | `v2/devin-domain` | `ac3e4d9` | 89 lane tests before integration; incremental collection, corrections, six outcomes and guarded confirmation |
| Realtime voice | `v2/devin-voice` | `bade0e0` | 30 offline voice/audio tests; paced output, stale-generation rejection and complete-response tracking |
| Gateway/Jev | `v2/devin-gateway` | `7156e43` | 18 Python and 15 Jev tests plus TypeScript checking |
| Operator UI | `v2/devin-dashboard` | `d0082d1` | 41 UI tests plus coordinator-run real Chrome smoke |
| Evals/data | `v2/devin-evals` | `1a2083e` | 62 lane tests and all 18 synthetic outcome/language fixtures |
| API/auth/integration | `feat/voice-v2` | `f98efc0` and final domain integration | Authenticated text sessions, browser tickets, protected evidence, standalone migration, final compatibility fixes |

## Checkpoints

| Checkpoint | Revision | Publication |
| --- | --- | --- |
| Initial laboratory | `72df603` | Pushed |
| Parallel plan | `f41167e` | Pushed |
| V2 independence and approved v1 removal | `df00253` | Pushed |
| Operator/API/provider/voice/evaluation integration | `f98efc0` | Pushed |
| Domain integration and final compatibility fixes | This checkpoint | Coordinator commits and pushes with this status update |

## Remaining external gates

1. Main-workspace `v2/.env` still needs `AI_GATEWAY_API_KEY`, `CARTESIA_API_KEY`, `V2_OPERATOR_TOKEN`, a verified `V2_CARTESIA_VOICE_ES`, and paid opt-in. Deepgram, ElevenLabs and Prosper credential names are already configured. Values must never enter Git or logs.
2. Jev deployment is outside the authorized code-only scope. Its configured HTTPS endpoint and matching service token are still absent; the service code is included and verified offline.
3. Real voice/model quality, language latency, carrier acceptance and paid end-to-end behavior remain unverified until the missing configuration is supplied. Socket-send observations are not playback acknowledgements or official grades.
4. Docker is not installed on this machine, so the container build was not executed.
5. Voice cloning, audio-model duels, a genuine independent holdout corpus and automatic improvement promotion remain outside this functional stock-voice delivery. Arbitrary date/time ranges ask for clarification rather than silently dropping constraints.
6. Operator reconciliation is available in the store API for unresolved action outcomes; a reconciliation UI is not implemented. Do not blindly replay unresolved actions or reset the budget ledger.

Keep code delivery, mocked/fixture evidence, provider-backed acceptance and official scoring distinct. No deployment or spending-limit increase is implied by these commits.
