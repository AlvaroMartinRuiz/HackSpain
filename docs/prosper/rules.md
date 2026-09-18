# Scoring

**Version 2.0-draft · 17 September 2026 · HackSpain, 18–20 September 2026**

This page is the automatic score: what passes a case, how points are counted,
what the limits are, and what happens when a call fails. The jury's *final
boss* is scored separately and is described in
[what the challenge is](challenge.md#who-wins).

## What passes a case

A case passes or it fails. There is no partial credit within a case — not for
a field, not for most of a name, not for an id that is one character out.

A case passes if the **list of actions** you submit matches one the case
accepts, after [normalization](scoring.md). What you submit and what each
action carries is in [the contract](contract.md).

**Doing nothing is not silence.** A call whose right answer is "this cannot be
booked" still submits a \`NO_ACTION\` carrying the reason. An empty list, or no
submission at all, is always wrong — otherwise an agent that crashed would
score the same as one that correctly refused.

**Nothing about the conversation is scored here.** Voice, manner, how personal
the call felt and how well the load was spread all belong to the jury. See
[the scheduling guidelines](clinic-api.md#scheduling-guidelines) for what to do
with them.

**More than one answer can be correct.** "The earliest appointment with a GP"
has three right answers when three GPs are free at the same minute. A case
carries the **set** of acceptable outcomes and your submission passes if it
matches any member. Scoring stays binary: it is membership, not partial credit.

Expected answers are computed through the same availability use case you call,
so a case can never expect an appointment the API would not have offered.

## The two lanes

**Practice** dials one published case, answer and all. As often as you like
within the rate limit. It scores nothing.

**Run All** is the scored lane: four private cases for every scored problem
that is currently open, dialled 10 at a time. You choose nothing about it —
the point of it is the whole open set. Take as many as you like, one at a
time. It grows as problems open: the roster starts at two scored problems
(8 calls, a couple of minutes) and ends at seventeen (68 calls, about
eighteen). [The problem set](problems.md) says what is open now.

Private cases are generated per run and their answers are never published.

While scoring is open, a private case tells you **whether it passed, whose
failure it was, and a failure signal** such as \`missing_record\` or
\`record_mismatch\`. It does not tell you which field lost, and it carries no
transcript and no audio. Those open at the **reveal — Monday 21 September,
00:00 Europe/Madrid** — after the event has ended. The expected values are never
published, before the reveal or after it.

Practice is the lane you debug in: a published case shows you its answer, the
fields your record lost, the transcript and the recording, straight away. See
[recordings](#recordings).

## Points

Every scored problem carries a **difficulty weight from 1 to 5**, published on
the problem list and in [the problem set](problems.md). A problem's score is
the fraction of its four cases that passed — 0, .25, .5, .75 or 1 — times that
weight. **Your score is the sum of those. There is no percentage and no
denominator.**

\`\`\`
points = sum over problems of (its pass fraction × its weight)
\`\`\`

Pass every case of *The Real Call* and 5 points go on the board; pass every
case of *The Simple Booking* and 1 does. The most the full roster can give is
**49**.

A sum rather than a percentage because the set opens across the weekend. Under
a percentage, the same agent's score would fall every time we released a
problem it had not been built for — it would look like it was getting worse
while it sat there unchanged. A sum only ever grows as you solve more, and a
score from Friday means the same thing on Sunday.

**A problem nobody attempted scores nothing**, exactly like one that was
dialled and failed. There is no credit for what you did not get to, so
running only the problems you are good at buys nothing. A call that never
produced a submission is an attempted, failed case: silence is never cheaper
than a wrong answer.

The leaderboard ranks each team's **best** Run All. Not latest, which would
punish experimenting late on Sunday; not cumulative, which would punish
iterating at all. Best rewards the thing the weekend is for. It is not free of
luck — four cases per problem is a sample — so the board shows how many runs
backed a score beside it. Once the whole roster is open, a 70%-correct agent
has no realistic chance of a perfect 49 across 68 calls.

Problem 2 scores nothing at all — it carries no weight and Run All never
dials it. Practice calls never score either.

**Problems open progressively.** The set is released as each problem is
verified end to end. What you have already earned is yours: opening a new
problem never changes the score of a run that was taken before it, because
there is no denominator for it to move.

[Attributed harness failures](#when-a-call-fails) are excluded rather than failed.

## Call limits

Every call is capped at **three minutes** — an agent that cannot book in three
minutes has failed. A call is also cut off if it takes too long to connect or
goes quiet, which means **no audible audio** from your agent: streaming silence
keeps the socket open but counts as saying nothing, and the call is cut off and
attributed to your agent.

A call cut off this way is still an attempt. Without an accepted record it
scores nothing.

## What is not scored

- Voice quality, accent, naturalness, politeness, conversational style.
- Transcription accuracy on its own, or spelling aloud.
- The number or order of questions, tool calls or confirmations.
- Model choice, architecture, token usage, provider cost.
- Speed. Limits apply and can stop a valid record arriving, but being fast
  earns nothing.

A good conversation does not rescue a wrong record, and a clumsy one does not
fail a right one. The one exception is
[problem 14](problems.md#14-adversarial-and-privacy), where the transcript
is checked for leaked patient data.

## Corrections and disputes

A rule change is announced to every team, with the old and new wording, the
reason and the effective time, before it takes effect. The wire and the
submission schema stay backward compatible for the weekend. A change to matching, eligibility, points
or deadlines is a scoring change even when it is a bug fix.

If a correction affects results already recorded, the decision on rejudging or
exclusion is published for all affected teams before the standings move.

For a dispute, give an organiser your team, run and call ids, the rules
version, the rule you expected and what you observed. See
[recordings](#recordings); scored-case evidence is not released while
scoring is open.

The wall freezes Sunday 20 September at 06:00 Europe/Madrid. Only runs
completed at or before that instant count. Equal scores share a rank (1, 1, 3).

Private-case detail opens to each team at the reveal, Monday 21 September at
00:00 Europe/Madrid — after the stage final, so nothing can leak into it.

## When a call fails

Attribution is deterministic. No LLM arbiter decides whether a failure counts.
Each settled case retains its comparison and observed failure signals.

| Evidence | Attribution | Run treatment |
| --- | --- | --- |
| Matching record, no failure signals | none | Case passes |
| Missing/mismatching record, no infrastructure signal | agent_issue | Case fails |
| Endpoint unreachable, malformed agent message, or clean early hang-up | agent_issue | Case fails |
| No audible audio from your agent for the silence window | agent_issue | Case fails |
| Wall-clock limit, turn cap, unexplained disconnect, unidentified pipeline error | inconclusive | Case fails; evidence is available for investigation |
| Identified harness STT/LLM/TTS error, or confirmed local socket defect | harness_issue | Entire run is voided |
| Confirmed harness defect and independently observed agent failure | mixed | Entire run is voided |

A harness verdict requires a concrete **component, problem, and fix** attached to
a recognised harness signal. An error label alone is not enough. A record
mismatch or missing record during a harness failure does not independently prove
an agent defect. TTS throttling reported through its error frames counts as a
provider defect; slow speech alone does not prove throttling. A socket disconnect
does not identify which host or network failed. \`ENETDOWN\` on the judge host does.

A voided run contributes no score and releases the cooldown for its own mode.
It never triggers a silent rerun. The owning team's run API response contains
\`status: "voided"\`, a notification, and per-call attribution and signal codes.
Request a replacement run explicitly. A later run can still occupy the team's
active slot or start a new cooldown. Retrying delivery of an old settlement does
not reset that later cooldown.

For evidence, organisers use the existing \`X-Admin-Key\` with
\`GET /admin/teams/{team_id}/runs/{run_id}/evidence\`. It returns retained error
details and concrete defects. This route is absent from the public OpenAPI
schema. Team responses expose only identifiers, attribution, and fixed signal
codes: raw provider errors, field names, private case contents and transcripts
are never included in attribution feedback.

## Recordings

By connecting an agent to El Turno, you agree that calls are recorded as audio
and timestamped transcripts for debugging, judging, dispute resolution and the
Sunday stage; all practice and scored recordings are retained after the weekend,
with no automatic deletion schedule.

Other teams can never read your recordings, and you can never read theirs.

### What you can read, and when

Which lane the call came from decides this, not who you are.

**Practice calls are open as soon as they end.** The case was published with its
answer, so there is nothing left to protect: the transcript, the fields your
record lost and the audio are all on your team page immediately.

**Scored calls stay closed until the reveal — Monday 21 September, 00:00
Europe/Madrid.** Until then a private case shows you whether it passed, whose
failure it was and a failure signal, and nothing else: no transcript, no audio,
no per-field comparison. At the reveal the transcript and the audio open to your
team.

**The expected answer to a private case is never published**, before the reveal
or after it. Which field you lost is feedback; the value it wanted is the answer
key.

Organisers are not on this clock — they can read any team's private-case detail
throughout the weekend, because they are who a verdict is disputed to and that
has to be answerable before Sunday rather than after it.

### Transcripts

Transcripts are machine-generated. Their timestamps mark when recognised or
spoken text reached the harness, not exact word boundaries. Audio is the source
to consult when a transcript mishears a name, number or other detail.

## Still to be decided

Organisers confirm these before scored calls open. Until then, nothing in
these docs implies an answer:

- How stage-final places are settled when qualifiers tie.
- The announcement channel for corrections, and who owns a dispute.
