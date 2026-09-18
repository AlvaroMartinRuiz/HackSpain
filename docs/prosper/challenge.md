# What Is the Challenge?

## Who we are

Prosper builds voice AI for healthcare. Our agents pick up the phone for
clinics and hospitals all day, and the single biggest thing those calls are
about is **scheduling**.

Scheduling sounds simple until you look at a real clinic. Every appointment
is the intersection of a patient, a provider, a location, a visit type, an
insurance, a referral, a piece of equipment and a calendar — and every one of
those carries rules. Who is allowed to see whom. Which visit types need a
double slot. What counts as a new patient. Which of the four Maria Garcías
born in the same decade is the one on the phone. A voice-to-voice model on its
own does not know any of that, and it will happily invent an answer.

That is why the interesting work here is **engineering**, not prompting. The
teams that do well at this problem in the real world are the ones that build
around the model: retrieval over patient records, tools that can only return
real availability, deterministic checks before anything is written, state that
survives a caller changing their mind on the third turn, and observability good
enough to explain why the agent said what it said. We encourage you to do
exactly that this weekend — treat the voice model as one component of a system
you designed, not as the system.

And the weekend is not only about passing cases. Half of what makes a voice
agent usable is the **platform around it**: how the call is orchestrated, what
you can see while it is happening, and what you can learn from it afterwards.
Build that too — it is judged.

## The challenge

Build a voice AI agent that answers **inbound scheduling calls** for a clinic,
the way a receptionist would.

Your agent picks up, talks to the patient, works out who they are and what they
need, and uses the clinic's EHR — an electronic health record system, the
healthcare equivalent of a CRM — to get it done. That means finding the patient
and their chart, checking real availability, and creating, moving or cancelling
the appointment. Some calls should not end in a booking at all, and recognising
those is part of the job.

The challenge is posed as **18 problems**, each with its own **persona** who
calls your agent. They cover the range a real front desk sees: a
straightforward booking, an ambiguous patient match, a caller who changes their
mind mid-call, someone asking for something the clinic cannot give them, a bad
line, a caller who is not the patient. You have to handle all of them — a
single-path happy-flow agent will not get far.

You submit your agent, and we call it. We run every persona against it and
check what your agent actually did against what the case accepts. Each problem
you handle correctly earns points, and those points place you on the
**leaderboard**.

## Who wins

**The leaderboard decides the main prize.** Every persona your agent handles
correctly adds points; the team on top when the wall freezes wins.

**Checkpoints.** Twice over the weekend we freeze the board and hand a prize to
whoever is leading at that moment, so being early pays. The desk announces the
windows; a schedule published before the event reads as a commitment nobody
made. To be in the running you need a registered team, a reachable endpoint and
your key configured — see [get on the phone](quickstart.md). Organisers place
smoke calls at every endpoint during the weekend to shake out problems early;
those are practice and cost nothing if they fail.

**The final boss.** The jury calls your agent themselves and scores it
separately, added to your total. Nothing here is automated: the same panel
judges every team, and the desk announces the weights. Where a criterion touches
something the board already scores — triage, languages, a caller who is not the
patient — the jury is judging *how it was done*, never whether the record came
out right.

1. **The patient experience.** Whether it sounds like a person: pace and warmth,
   whether you can interrupt it and change your mind on the third turn, whether
   it repairs a mishearing instead of guessing loudly, and how long the caller
   spends for what they got. An agent that invents a slot, a doctor or a rule to
   keep the conversation moving is marked down hard, whatever it sounded like.
2. **How personal it gets.** Whether the clinic sounds like it knows who is
   ringing. The chart is used before it is asked for — a caller seen eleven
   times is not asked whether they have been here before — and identification
   feels like recognition rather than an interrogation.
   [The scheduling guidelines](clinic-api.md#scheduling-guidelines) are written
   for this criterion.
3. **The platform you built around it.** Judged on what you show, driven live in
   front of the jury rather than on a slide: how a call is actually run, what you
   can see while one is in flight, whether you can answer "why did it say that?"
   afterwards, and whether ten concurrent calls hold up. We are deliberately not
   scoping this one — surprise us.
4. **Safety and boundaries.** An agent that is charming and unsafe scores worse
   than one that is plain and careful. It does not practise medicine, it
   escalates before it improvises, it knows who it is talking to and what it may
   read back, and it stays in character when the jury tries to talk it out of its
   own rules.
5. **Language and reach.** The board checks the right language was used; the jury
   checks it was used *well*. Code-switching mid-call without a restart, Spanish
   names and national ids said the way a person says them, and patience with a
   caller who is elderly, hard of hearing or on a bad line.
6. **Engineering rigour.** How you know it works, separately from whether it
   worked on the jury's call: your own evaluation harness, variance across
   repeated runs, the failure modes you can name, and what a call costs you in
   money and seconds.
7. **The jury's discretion.** Held back deliberately — something nobody asked
   for, an idea worth stealing, a call that made the room go quiet. The panel
   awards it on its own judgement and justifies it against nothing above.

**What does not earn points:** the leaderboard score, the size of the diff, the
model you picked in itself, and anything you cannot show working. A criterion the
jury cannot observe on a call or in a demo is not scored, so demo what you built.

## Start here

[Get on the phone](quickstart.md) takes you from nothing to an agent answering
a judged call. Then [the clinic](clinic-api.md) for the rules you are working
inside, and [scoring](rules.md) for what passes.
