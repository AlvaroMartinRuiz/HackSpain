# The clinic

**Clínica Arenal** is a read-only EHR. Three sites, twelve providers, six
specialties, eleven appointment types, ten insurance plans, close to 3,000
patients with real visit histories, and a fixed calendar. It is generated once
and is identical for the whole event, for every team and every call. Cache it
freely.

Every endpoint needs \`X-Api-Key\`. Every field of every one of them is in the
[API reference](api.md), so it is not repeated here — this page is the
clinic's *rules*, and the traps in them, which no schema can state.

Three of them ask about one caller:

| | |
|---|---|
| \`GET /api/v1/directory\` | Who is calling. \`name\`, \`national_id\`, \`phone\`, \`date_of_birth\` |
| \`GET /api/v1/availability\` | What they may book and when. \`date_from\`, \`date_to\`, plus \`provider_id\` or \`specialty_id\`, and optionally \`location_id\`, \`patient_id\`, repeated \`insurer\` |
| \`GET /api/v1/patients/{patient_id}/appointments\` | The caller's diary, earliest first. \`when\` is \`upcoming\` (the default), \`past\` or \`all\` — the past is there to be read back to a caller. The only source of an \`appointment_id\` |

The rest are the catalogue behind those searches — the same catalogue
**Clinic records** on your dashboard draws from. They take no parameters and
never change during the event, so pull them once at start-up and hold them:

| | |
|---|---|
| \`GET /api/v1/clinic\` | All of the below in one call, plus the bookable window and the standing restrictions with the decline reason each one carries |
| \`GET /api/v1/providers\` | Who works here: specialty, languages, the types they perform, where and when they sit, the plans they take and refuse, any leave |
| \`GET /api/v1/locations\` | The three sites: address, opening hours, who sits there, which plans cover them |
| \`GET /api/v1/specialties\` | Age window, whether a referral is required, which plans cover it. The ids \`availability?specialty_id=\` takes |
| \`GET /api/v1/appointment-types\` | Duration, the specialty it belongs to, and which patient it is for |
| \`GET /api/v1/insurance-plans\` | What each plan covers, where, and which providers take it |

**There is no booking endpoint.** Nothing you call here reserves anything —
you report what you decided through [the contract](contract.md). Another team
practising cannot take a slot from you.

**Reading the catalogue is not the same as knowing the rules.** It tells you
that dermatology needs a referral and that Caser does not cover it; it does not
tell you that this caller has neither. That part is a receptionist's judgement,
and the difficulty is noticing a rule applies to the person on the line — never
discovering the rule exists.

## Six things worth knowing

**Submit the record's name and id, never what the caller said.** A nickname or
a misheard surname can still find a patient; it is not a legal name.

**An exact field that does not match excludes the patient.** It filters, it
does not downrank. That is what makes \`name\` plus \`date_of_birth\` the tool for
separating two people with the same name — and why a misheard national id
usually returns nothing. Some ids differ from another patient's by one digit,
so a confidently wrong id can return a confidently wrong person. Confirm on a
second field.

**\`phone\` takes the caller id as it arrives.** The number is folded to its
nine national digits before it is compared, so \`+34612345678\`, \`0034612345678\`
and \`612345678\` are one query — the \`from_number\` on the wire needs no
reshaping. A hit is the patient whose line it is, which is not always the
patient being booked for.

**Availability answers without being asked to book.** Restriction metadata
comes back whether or not there are slots, and \`blocked\` names the standing
rule that stopped a provider. Empty \`slots\` with empty \`blocked\` means the
calendar is simply full — a different answer.

**Naming a plan is the only way to be quoted against it.** Leave \`insurer\` out
and the search prices against the single plan on the patient's record. A
second plan is nowhere in the data; asking on the call is the only way to find
it. See [problem 17](problems.md#17-the-second-policy).

**Every patient record carries a \`note\`, and a chart behind it.** The note is
what a receptionist left on the record, and no two read alike. It carries how
their history actually runs — how recently they were in, how many visits, with
which doctor, at which site ("Not been in since April 2024. Seen once, by Dr.
Pablo Requena. Every visit so far has been at Arenal Norte.") — and how to talk
to them ("hard of hearing — speak slowly", "usually comes with a relative").
The appointments endpoint returns their past visits as well as their upcoming
ones. Neither is scored, and neither is decoration: this is the
material the jury's *final boss* judges a personal call on. See
[scheduling guidelines](#scheduling-guidelines).

## Sites

Three, all in the **Clinic records** tab: Centro, Norte, Sur. Only Centro
opens on a Saturday. Nothing opens on a Sunday. Coordinates are published in
\`/availability\`'s location data and are the ground truth for
[problem 15](problems.md#15-the-nearest-site): the right site is the one
at the smallest straight-line distance that can actually serve the request.

## Specialties

Six, in Clinic records. The 14th birthday is the age boundary, in months,
with no gap and no overlap: every age has exactly one correct specialty for a
general complaint. A patient's held referrals are on their directory record.

## Providers

Twelve, in Clinic records — names, specialties, schedules, languages. Three
traps the table alone won't tell you:

**Dr. Requena is on leave 14–30 September** (sick leave), which covers the
whole event. A caller who asks for him by name has to be moved.

Two near-miss pairs make a spoken name genuinely ambiguous, and each pair sits
in a different specialty: **Sáez** (general practice) / **Sáenz** (paediatrics),
and **Iglesias** (dermatology) / **Iglesia** (orthopaedics). Ask which.

**D. Álvaro Cid, not Dr.** Physiotherapists are not doctors, and the title is
part of the name you submit.

Language constrains a booking only in [problem 11](problems.md#11-languages).
Everywhere else, assume any provider can take the call.

## Insurance plans

Ten, with their coverage and the standing refusal rules, in Clinic records.
Two interactions are worth knowing going in: ASISA covers physiotherapy but
only at Centro and Norte, and the only physiotherapist sits at Sur, so an
ASISA patient can never book physio at all. Adeslas covers no gynaecology,
and there is one gynaecologist, so there is nowhere to redirect an Adeslas
patient to.

**Dra. Iglesias does not take DKV; Dr. Vilar does** — so a DKV patient asking
for her by name is a redirect, not a refusal. Every other provider takes all
ten plans.

\`privado\` is self-pay, and it is a plan a patient holds or does not — not a
fallback. An uncovered patient is refused.

Patients hold **one or two** plans. Only the first is on the directory record;
a second exists to be asked for on the call. See
[problem 17](problems.md#17-the-second-policy).

## Appointment types

Eleven, in Clinic records. Each carries a one-line \`guidance\` saying when it
is the right one, and \`/availability\` returns it on the type it picked, so
the hint is on the wire and not only in the catalogue. Exactly one is right
for any booking, and it follows from two facts on the record, never from the
conversation: the **specialty** being booked, and **\`has_visited_before\`** on
the patient. A specialty's own types win over the two universal ones
(\`first_visit\`, \`review\`); gynaecology has its own review only, so a new
gynaecology patient books the universal \`first_visit\`.

The trap: two specialty pairs run the same minutes as the universal ones, so
what separates \`review\` from \`dermatology_review\` is the id alone. An agent
that hard-codes \`review\` for every follow-up submits a real slot under a type
that specialty does not offer, and fails a case it understood perfectly.

You do not have to work this out yourself: every \`/availability\` response
names the one type that fits the patient and specialty it was asked about,
as \`appointment_type\`, and every slot in it carries that type's id. Submit
that id. Ids are compared exactly, and the same slot under the wrong type
fails the case.

## What the patient has already been to

The same endpoint answers for both halves of a diary, and \`when\` picks which.
It defaults to \`upcoming\`, so a call that lists what the caller has booked
sees only what they can still act on.

\`when=past\` returns the visits behind them: **2024 and 2025**, up to eight of
them, for almost everyone the clinic has seen before. How a chart runs differs
per patient and is worth reading: some have been coming throughout, some came
for a short course of treatment and stopped, and plenty were last in over a
year ago. They are there to be read back — "you last saw Dr. Requena in March"
— and nothing else. **A past
visit cannot be cancelled or moved**, and its \`appointment_id\` is not an
answer to [problem 8](problems.md#8-change-and-cancel); only an upcoming one
is. A patient whose record says \`has_visited_before\` is \`false\` has no past
visits at all, and a handful of the youngest patients have none either.

The history is deliberately older than this year, so it never disagrees with
what a capped plan says has been spent against it.

## Calendar

Slots run **7 September – 16 October 2026** in 15-minute steps. Availability
outside that range is \`422\`; a span longer than 14 days is too. Visit history
reaches about eighteen months further back; it is readable through the
appointments endpoint and is not bookable.

**No two providers are equally busy.** Diaries run from roughly 40% to 72%
full, provider by provider, and that is deliberate — the one gynaecologist has
full days, the one physiotherapist has room. Which doctor you send a patient to
is therefore a real decision, not a coin flip.

**Monday 12 October is Fiesta Nacional and the whole network is shut.** It is
the one published closure day, and it landing on a Monday is what makes "first
thing Monday" a trap.

Dates resolve against **the moment your call connects**, in Europe/Madrid —
not against your machine's clock and not against a fixed anchor. "Next
Thursday" is whatever it is when the phone rings.

**Nothing is booked for the same day.** "The earliest appointment" means the
earliest from the day after the call. \`/availability\` still lists what is
free later today, because the calendar is what it is, but a slot on the day
of the call is never an accepted answer.

## Scheduling guidelines

None of this is scored. The leaderboard only ever asks whether the actions you
submitted match the ones the case accepts, and the caller's own words are what
decide that. These are the things a good front desk does *on top* of getting
the record right — and they are what the jury's final boss is looking for.

**Register before you book.** A caller the directory does not know cannot be
booked: there is no \`patient_id\` to book against. Register them first, from
what they tell you on the call, and treat the demographics as the answer for
that case. See [the contract](contract.md).

**Read the chart before you ask.** Every record carries a \`note\` and a visit
history. A caller who has been seen eleven times should not be asked whether
they have visited before, and should not be told about a first-visit slot. Use
the history to confirm an identification too: a chart with nothing on it under
a name the caller says is a regular is a signal you have the wrong person.

**Be personal, but let the caller decide.** "Dra. Ortiz usually sees you — her
next free slot is Thursday, or I can get you in tomorrow with Dr. Sáez" is the
answer that wins on both counts. Silently booking the usual doctor when the
caller asked for the soonest appointment is not: it fails the case. The note
and the history are context for the conversation, never an instruction that
outranks what the caller asked for. Nothing in a note is ever a scheduling
preference for exactly this reason.

**Respect the practice's rules, every time.** Age boundaries, referrals, the
insurance matrix, site coverage, opening hours, the closure day, no same-day
booking. Most of them are invisible until you look: \`/availability\` names the
restriction that bit in \`blocked\`, and a refusal that names the right rule is
a correct answer where a booking would have been wrong.

**Spread the load.** When several providers can serve a request, the busiest
one is rarely the right answer. An agent that always offers the first tied slot
from the same doctor produces a clinic where one provider is buried and another
is empty. Look at what the specialty's diaries actually look like and place the
patient accordingly — while still respecting the caller's ask, which comes
first.

**Then go further.** This is the half of the challenge with no answer key.
Remember what happened on a caller's previous calls and open with it. Read back
the appointment the way a person would. Notice that a caller has an appointment
next week before they tell you. Ask the question the note implies. Anything
that makes the person on the line feel known is worth building — and worth
showing the jury.
