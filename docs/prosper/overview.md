# El Turno — the short version

One page, no technicalities. Everything here is expanded somewhere else, and
every section says where.

## What you are building

A **voice AI agent that answers inbound scheduling calls for a clinic**, the
way a receptionist would.

Someone rings. Your agent picks up, works out who is calling and what they
need, looks them up in the clinic's records, finds real availability, and books,
moves or cancels the appointment. Some calls should not end in a booking at all
— the clinic cannot do it, the caller needs a doctor now, the rules say no —
and recognising those is as much a part of the job as booking well.

The voice model is one component of a system you design, not the system. The
teams that do well build around it: real lookups, real availability, checks
before anything is written, state that survives a caller changing their mind,
and enough visibility to explain why the agent said what it said.

## How the weekend runs

**Friday 18 → Sunday 20 September.**

1. **Register your team**, get your key, and stand up an endpoint we can call.
   The starter kit gets you a talking agent in minutes; everything after that
   is yours. The desk hands you a debit card with **€100 on it, one per team**,
   to pay for whatever your agent runs on.
2. **Build and rehearse.** You can dial yourself as often as you like against
   published practice cases, answers included.
3. **Run for score** when you think you are ready. We call your agent with
   every problem, check what it did, and your points go on the leaderboard.
4. **Checkpoints.** Twice over the weekend the board freezes and prizes go to
   whoever is leading. Being early pays.
5. **Sunday: the final boss.** The jury calls your agent themselves, and you
   show them what you built.

→ [Get on the phone](quickstart.md)

## What the callers throw at you

**Eighteen problems**, each with its own persona who rings you up. Each one
isolates a single thing that makes a real front desk hard, sitting on top of
the same ordinary booking:

- The straightforward booking, and ten of them at once.
- A caller the records do not know yet, and a caller who matches four people.
- Someone asking for a specific doctor, a specific site, or "the soonest".
- Vague times — "next Thursday", "first thing Monday" — that have to resolve.
- Requests the clinic's rules forbid, which must be refused for the right
  reason.
- A full diary with nothing free.
- Changes and cancellations.
- A parent calling for a child, a daughter for her father.
- Someone who should be sent to a doctor, not a calendar.
- Callers not speaking English, including other languages of Spain.
- A terrible line, a caller who interrupts and corrects and changes their mind,
  and someone trying to talk your agent into something it should not do.

→ [The 18 problems](problems.md)

## How you are scored

Two things, added together.

**The leaderboard — automatic, and brutally literal.** After each call your
agent tells us what it did. Either that matches what the case accepts, or the
case fails. There is no partial credit, no points for a nice conversation, and
no credit for nearly. A call that correctly refuses still has to say so; silence
is always wrong.

**The jury's final boss — everything the leaderboard ignores.** They call you
themselves and judge the call as a person on the phone would: how it sounds,
how it handles being interrupted, whether it feels like the clinic knows who is
calling. Then they judge what you built around it — how a call is orchestrated,
what you can see while it is happening, what you can learn from it afterwards,
and whether you can show any of it working. Safety, language, and how you know
your own agent works all count.

→ [Scoring](rules.md) · [Who wins](challenge.md#who-wins)

## What actually wins

- **Correctness first.** The board is the main prize, and it only rewards
  getting the record exactly right.
- **Then the call.** A correct agent that is unpleasant to talk to loses the
  half of the marks the jury holds.
- **Then the platform.** The part with no answer key. A live console, a phone
  number the room can ring, a way to see why the agent did what it did. We are
  deliberately not scoping it.

## Start here

[Get on the phone](quickstart.md) takes you from nothing to an agent answering
a judged call. After that: [the clinic](clinic-api.md) for the world you are
working inside, [the call contract](contract.md) for how your agent talks to
us, and [scoring](rules.md) for what passes.
