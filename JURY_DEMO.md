# Jury demo — Socket Wizard

Five minutes. Live first. Slides never.

The jury calls the agent themselves and then watches the floor. Correctness is already on the board; this script is how the call *felt*, how personal it was, and whether the platform around it is real.

## Setup (T-10 min)

- [ ] Process up, `RELOAD=false`, no file saves
- [ ] `/health` ok, Smart Turn warmed, TTS cache warmed
- [ ] Dashboard on a 1920×1080 screen: `/` or `/demo`
- [ ] Talk page ready with headphones (or the agent's speech barge-ins)
- [ ] Dry run **checked** unless this is a scored line
- [ ] Outbox open (`data/emails/`) or FOLLOWUP_COPY inbox
- [ ] Example call (`/demo` → Open example call) loaded as Plan B
- [ ] Concurrency button idle (it only starts dry-run sockets)

## 0:00–0:25 — The problem

> A real patient doesn't call a clinic following a script. They interrupt, they change their mind, they are already on file.

Show Socket Wizard: live calls, accepted records, languages, median response, peak concurrency.

## 0:25–2:00 — Live call

Talk to the Agent, dry run.

Aim for:

1. Identify a known patient from the synthetic directory (name + DNI).
2. Chart used before it is asked for — visit count, usual doctor, insurer.
3. Find a real slot. If a named doctor does not take the plan, wait; do not invent an alternative until the caller asks.
4. Invite a juror to interrupt.
5. Change doctor, site or time.
6. Optional code-switch (ES → CA or EN).
7. Confirm. BOOK.

Watch **Patient journey** update live. Do not narrate every tool.

If the line is noisy: repair, don't guess.

## 2:00–2:40 — Closed loop

Open the follow-up on the Record tab (or the saved HTML).

Point at: appointment details, **Add to calendar / ICS**, **Directions**. No booking reference. Submission already succeeded; mail cannot unsay it.

## 2:40–3:20 — Why this decision?

Inspector → **Why**.

Request, constraints, insurance, the option the caller accepted. Then, if they ask, Decisions / Tools / Record underneath.

If a field is missing: “No additional decision trace available.” Never improvise a why.

## 3:20–4:00 — Try to break it / safety

One card, one real attempt. Prefer:

- Privacy attack, or
- Emergency red flag, or
- Call for someone else

The agent must stay in character. Charming and unsafe loses.

## 4:00–4:30 — Concurrency

**Demo concurrency** (dry-run only). Grid: caller, language, intent, listening/thinking/speaking, time. One human Talk session can sit next to the silent sockets.

Never fire scored submits from this button.

## 4:30–5:00 — Engineering

Reliability: accepted records ≠ scored passes. Median / p90. Replay. Deterministic domain engine: the model conducts, code decides the record.

Close:

> Socket Wizard isn't a chatbot that happens to book appointments. It's an AI front desk that listens, understands the clinic, acts safely and can explain every decision.

## Plan B

| Failure | Move |
| --- | --- |
| TTS / voice keys | Rehearse as text; show transcript + journey + why |
| Email | Open `data/emails/*.html` and `*.ics` |
| Internet / clinic API | `/demo` example call (Elena García, synthetic) |
| Live call dies | Do not restart mid-demo if other calls are up; switch to example + replay |
| Dashboard blank | `/console` classic view, or refresh once |
| Concurrency button 409 | Wait; one burst at a time |

## What not to do

- Do not invent a slot, a doctor, or a clinic rule to keep talking.
- Do not uncheck dry run unless the desk asked for a scored line.
- Do not restart the process during a live jury call.
- Do not show `.env`, tokens, or provider dashboards with keys.
