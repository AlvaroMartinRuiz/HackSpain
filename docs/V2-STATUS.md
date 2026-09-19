# V2 delivery status

The status board on `feat/voice-v2` is authoritative. Copies on task branches are snapshots. Only the coordinator edits this board; lane owners report through their draft PRs.

## Current state

- Priority: functional product with stock voices. Voice cloning is last.
- Integration branch: `feat/voice-v2`.
- Code baseline: `72df603`.
- Active implementation launch revision: `b32caed`. Five isolated agent worktrees were created from this status-only successor to `f41167e`; the runtime baseline is unchanged.
- Coordination plan: [V2-PARALLEL-WORK.md](V2-PARALLEL-WORK.md).
- Assignment mode: five parallel implementation agents with isolated worktrees; coordinator owns integration and serial paid validation. The user explicitly requested removal of v1 and code delivery without deployment.
- V2 is now the sole application. Shared domain/client/audio utilities have been migrated and v1 removed in `df00253`. Operator text and browser voice APIs, the dashboard, provider adapters and persistent evaluation/history are integrated. Simulation/practice remain the defaults; live carrier operation requires explicit submission and release-approval flags. No deployment, paid provider acceptance or scored submission has occurred.
- Provider credentials were supplied separately to the coordinator. Values do not belong in this repository or lane briefs. Real provider/deployment checks are still required; gateway access is not equivalent to Vercel deployment access.
- API spend from this checkpointing/planning work: $0. Teammates' unrelated account activity is not measured here. Total previously authorized effort budget remains $30.

## Work board

| Lane | Owner | Branch | State | Next checkpoint |
| --- | --- | --- | --- | --- |
| A — Conversation/domain | Domain agent / coordinator verification | `v2/devin-domain` | Final verification | Incremental collection, six outcomes, corrections and confirmation tests |
| B — Realtime voice | Voice agent / coordinator verification | `v2/devin-voice` | Integrated `bade0e0` | 30 offline voice/audio tests passed before integration |
| C — Gateway/Jev | Gateway agent / coordinator verification | `v2/devin-gateway` | Integrated `7156e43` | 18 Python and 15 Jev tests plus TypeScript check passed |
| D — Operator UI | Dashboard agent / coordinator verification | `v2/devin-dashboard` | Integrated `d0082d1` | 41 offline UI tests and coordinator-run real Chrome smoke passed |
| E — Evals/data | Evaluation agent / coordinator verification | `v2/devin-evals` | Integrated `1a2083e` | 62 lane Python tests and 18 outcome/language fixtures passed |
| F — Integration/release | Coordinator | `feat/voice-v2` | API/browser integration verified | 123 assembled Python tests; authenticated sessions, single-use tickets, terminal status and audio-finalization fixes |
| Voice cloning | Deferred | Not assigned | After functional release | Owner recordings and explicit clone provisioning |

## Checkpoint log

| Checkpoint | Revision | Evidence | Push |
| --- | --- | --- | --- |
| CP1 — Initial v2 laboratory | `72df603` | 33 v2 Python tests; 4 Jev tests; TypeScript checks; `pip check`; publishable-key-prefix/template checks | Pushed to `origin/feat/voice-v2` |
| CP2 — Parallel work plan | `f41167e` | Ownership, handoff briefs, dependency order, budget and release gates | Pushed to `origin/feat/voice-v2`; frozen lane launch revision |

## Integration queue

Four tested lanes are integrated; the domain lane is finishing coordinator verification. The current assembled checkpoint passes 123 Python tests, 41 UI tests, 15 Jev tests with TypeScript checking, and a real headless Chrome smoke at desktop/mobile widths. The standalone migration passed all deterministic domain checks. Paid verification remains blocked on main-workspace credentials; Docker build verification is unavailable because Docker is not installed. No paid requests or deployments were made. Remaining integration order:

1. A0/E0/F0 contract and fixture checkpoints.
2. C gateway adapters and A workflow increments: validate functional text interaction.
3. B voice adapter increments with the same validated workflow.
4. D operator UI with E's persisted evidence and F's authenticated API.
5. Coordinator-run provider probes and cross-lane release checks.
6. Team review and explicit cutover decision; cloning afterward.

A lane's passing unit tests do not automatically make the integration branch or live service ready. Summaries must state whether evidence is mocked, fixture-based, or provider-backed. No destructive data migration, live endpoint switch, secret publication or spending-limit increase is authorized by a lane assignment.
