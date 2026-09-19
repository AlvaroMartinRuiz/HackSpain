# V2 delivery status

The status board on `feat/voice-v2` is authoritative. Copies on task branches are snapshots. Only the coordinator edits this board; lane owners report through their draft PRs.

## Current state

- Priority: functional product with stock voices. Voice cloning is last.
- Integration branch: `feat/voice-v2`.
- Code baseline: `72df603`.
- Parallel launch revision: awaiting publication of the task-plan checkpoint; do not fork from a moving branch until the coordinator records the launch SHA here.
- Coordination plan: [V2-PARALLEL-WORK.md](V2-PARALLEL-WORK.md).
- Assignment mode: undecided (teammates, subagents, or mixed). No subagents launched by this planning work.
- V2 remains a lab/practice implementation; no live cutover or scored submission has been enabled.
- Provider credentials were supplied separately to the coordinator. Values do not belong in this repository or lane briefs. Real provider/deployment checks are still required; gateway access is not equivalent to Vercel deployment access.
- API spend from this checkpointing/planning work: $0. Teammates' unrelated account activity is not measured here. Total previously authorized effort budget remains $30.

## Work board

| Lane | Owner | Branch | State | Next checkpoint |
| --- | --- | --- | --- | --- |
| A — Conversation/domain | Unclaimed | `v2/domain` | Ready for audit/contract proposal | Additive schema needs and failing parity tests |
| B — Realtime voice | Unclaimed | `v2/voice` | Ready for offline work | Codec, interruption and three-language stock routing tests |
| C — Gateway/Jev | Unclaimed | `v2/gateway` | Ready for offline work | Gateway/rubric tests; request coordinated live smoke window |
| D — Operator UI | Unclaimed | `v2/dashboard` | Ready against current API/mock DTOs | Reuse UI and list backend/ticket contract requirements |
| E — Evals/data | Unclaimed | `v2/evals` | Ready for offline work | Outcome fixtures, metric definitions and run-summary contract |
| F — Integration/release | Coordinator (Devin until reassigned) | `feat/voice-v2` | Preparing delegation | Freeze launch SHA, collect owners, schedule dependency checkpoints |
| Voice cloning | Deferred | Not assigned | After functional release | Owner recordings and explicit clone provisioning |

## Checkpoint log

| Checkpoint | Revision | Evidence | Push |
| --- | --- | --- | --- |
| CP1 — Initial v2 laboratory | `72df603` | 33 v2 Python tests; 4 Jev tests; TypeScript checks; `pip check`; publishable-key-prefix/template checks | Pushed to `origin/feat/voice-v2` |
| CP2 — Parallel work plan | Pending | Ownership, handoff briefs, dependency order, budget and release gates | To push after document verification |

## Integration queue

No lane PRs have been assigned or integrated yet. Proposed order:

1. A0/E0/F0 contract and fixture checkpoints.
2. C gateway adapters and A workflow increments: validate functional text interaction.
3. B voice adapter increments with the same validated workflow.
4. D operator UI with E's persisted evidence and F's authenticated API.
5. Coordinator-run provider probes and cross-lane release checks.
6. Team review and explicit cutover decision; cloning afterward.

A lane's passing unit tests do not automatically make the integration branch or live service ready. Summaries must state whether evidence is mocked, fixture-based, or provider-backed. No destructive data migration, live endpoint switch, secret publication or spending-limit increase is authorized by a lane assignment.
