# V2 delivery status

The status board on `feat/voice-v2` is authoritative. Copies on task branches are snapshots. Only the coordinator edits this board; lane owners report through their draft PRs.

## Current state

- Priority: functional product with stock voices. Voice cloning is last.
- Integration branch: `feat/voice-v2`.
- Code baseline: `72df603`.
- Active implementation launch revision: `b32caed`. Five isolated agent worktrees were created from this status-only successor to `f41167e`; the runtime baseline is unchanged.
- Coordination plan: [V2-PARALLEL-WORK.md](V2-PARALLEL-WORK.md).
- Assignment mode: five parallel implementation agents with isolated worktrees; coordinator owns integration and serial paid validation. The user explicitly requested removal of v1 and code delivery without deployment.
- V2 remains a lab/practice implementation; no live cutover or scored submission has been enabled.
- Provider credentials were supplied separately to the coordinator. Values do not belong in this repository or lane briefs. Real provider/deployment checks are still required; gateway access is not equivalent to Vercel deployment access.
- API spend from this checkpointing/planning work: $0. Teammates' unrelated account activity is not measured here. Total previously authorized effort budget remains $30.

## Work board

| Lane | Owner | Branch | State | Next checkpoint |
| --- | --- | --- | --- | --- |
| A — Conversation/domain | Domain agent | `v2/devin-domain` | Implementing | Six outcomes, partial collection, corrections and confirmation tests |
| B — Realtime voice | Voice agent | `v2/devin-voice` | Implementing | Pipecat transport, cancellation and actual presentation tests |
| C — Gateway/Jev | Gateway agent | `v2/devin-gateway` | Implementing | Bounded provider adapters and Jev contract regressions |
| D — Operator UI | Dashboard agent | `v2/devin-dashboard` | Implementing | Real run inspection, natural text sessions and microphone calling |
| E — Evals/data | Evaluation agent | `v2/devin-evals` | Implementing | Persistent run listing, outcome fixtures and honest metrics |
| F — Integration/release | Coordinator | `feat/voice-v2` | Migrating shared code and removing approved v1 files | Standalone v2 verification, API/auth/browser-ticket integration |
| Voice cloning | Deferred | Not assigned | After functional release | Owner recordings and explicit clone provisioning |

## Checkpoint log

| Checkpoint | Revision | Evidence | Push |
| --- | --- | --- | --- |
| CP1 — Initial v2 laboratory | `72df603` | 33 v2 Python tests; 4 Jev tests; TypeScript checks; `pip check`; publishable-key-prefix/template checks | Pushed to `origin/feat/voice-v2` |
| CP2 — Parallel work plan | `f41167e` | Ownership, handoff briefs, dependency order, budget and release gates | Pushed to `origin/feat/voice-v2`; frozen lane launch revision |

## Integration queue

Five agents are implementing their assigned lanes; no lane checkpoint has been integrated yet. The standalone migration/removal checkpoint passes 37 offline v2 tests, all deterministic domain checks, dependency checks, and the offline asset-list check. No paid requests or deployments were made. Integration order:

1. A0/E0/F0 contract and fixture checkpoints.
2. C gateway adapters and A workflow increments: validate functional text interaction.
3. B voice adapter increments with the same validated workflow.
4. D operator UI with E's persisted evidence and F's authenticated API.
5. Coordinator-run provider probes and cross-lane release checks.
6. Team review and explicit cutover decision; cloning afterward.

A lane's passing unit tests do not automatically make the integration branch or live service ready. Summaries must state whether evidence is mocked, fixture-based, or provider-backed. No destructive data migration, live endpoint switch, secret publication or spending-limit increase is authorized by a lane assignment.
