# Demo video — Socket Wizard

About two minutes. This is a story, not a tour of the repo.

Patients, DNIs, charts and appointments are the synthetic Clínica Arenal directory from the Prosper challenge.

## Voice-over spine

**0:00–0:10 — Problem**

A clinic phone is not a form. People interrupt, switch language, call for someone else, and they are already on file.

**0:10–0:25 — Product**

Socket Wizard. AI front desk for Clínica Arenal. Cut to the live floor: KPIs and agent cards.

**0:25–0:55 — One real call**

Caller on Talk (headphones). Identify Elena García (or another known synthetic patient). Patient journey: connected → language → identified → chart. Availability from the clinic, not the model.

**0:55–1:15 — It yields**

Interrupt. Change of mind. Dashboard shows the cut and a new search. **Why this decision?** Constraint (insurance / doctor) and the option the caller accepted.

**1:15–1:30 — Language / safety / personalization**

Either a code-switch, or the patient-context card (regular visitor, insurer, hearing note). Do not fake a red flag if you cannot show the shield from a real tool result.

**1:30–1:45 — Closed loop**

BOOK. Email confirmation. Calendar ICS. Directions to the real site address.

**1:45–1:55 — The floor scales**

Concurrency grid or Reliability (median response, peak concurrency, accepted records). One second of replay.

**1:55–2:00 — Close**

Socket Wizard listens, knows the clinic, acts safely, and can explain every decision.

## What to record

| Shot | Screen | Action |
| --- | --- | --- |
| A | `/` live | KPIs, empty or one idle agent |
| B | `#/talk` + call rail | Live Talk, journey updating |
| C | Inspector Why / Safety | After the change of mind |
| D | Email HTML + ICS | From Record tab or outbox |
| E | Demo concurrency or Reliability | Dry-run sockets only |

## What to say on the call (example)

Keep it short. Spanish is fine if that is the greeting.

1. “Buenos días, soy Elena García, DNI 12345678Z, quiero dermatología el martes por la mañana.”
2. Interrupt: “Mejor el Dr. Vilar.”
3. Confirm the slot the tools actually returned.
4. Optional: “Moltes gràcies.”

Use a patient that exists in the cached catalogue. If identification fails, stop the take; do not bluff.

## What not to risk on a take

- Unchecking dry run
- Restarting the server
- A named doctor who is on leave (the agent should warn and wait — good live, bad if you need a booking in 30 seconds)
- Emergency / privacy cards unless you have time for the shield to fire
- Provider dashboards, `.env`, tokens
- Invented booking references or medical advice

## Edit notes

- Burn in “synthetic challenge data” once, small, in the first five seconds if the organiser wants it.
- Cut dead air; keep interruptions.
- If live audio is messy, keep the dashboard journey in frame — that is the product shot.
