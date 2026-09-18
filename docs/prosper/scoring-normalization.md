# Scoring: the normalization table

Generated directly from the scorer's own normalization code, not hand-edited —
the examples below are the same ones that code is tested against, so this
table cannot say something the code doesn't do.

Scoring is binary per case: the actions you submit match a set the case
accepts, or the case fails. What follows is the tolerance — what's forgiven
and what isn't. The actions themselves are in
[the contract](contract.md); the points are in [the rules](rules.md).

## Where normalization applies

Ids are compared **exactly**. There is nothing to normalize about \`PR05\`, and
pretending otherwise would let a near-miss provider pass. Normalization
applies only where a human voice was in the loop: the demographics a
\`REGISTER\` captures for a patient who is not yet on file, and the moment of
the appointment. All the tolerance in scoring lives in those two places.

Every accent-insensitive fold below uses the same rule: Unicode NFKD
decomposition with combining marks stripped. That is a deliberate
simplification, not an oversight — it also folds "ñ" to plain "n", so a
surname spelled with and without its tilde is treated as the same submission.


## National id (DNI or NIE)

| Submitted | Normalizes to | Why |
|---|---|---|
| \`12345678-Z\` | \`12345678Z\` | dash stripped, letter already uppercase |
| \`12345678z\` | \`12345678Z\` | lowercase letter uppercased |
| \`1234 5678 Z\` | \`12345678Z\` | internal whitespace stripped, not just the ends |
| \`x-1234567-l\` | \`X1234567L\` | a NIE normalizes the same way |

## Captured patient name

| Submitted | Normalizes to | Why |
|---|---|---|
| \`José García López\` | \`jose {garcia, lopez}\` | accents stripped, case folded |
| \`José López García\` | \`jose {garcia, lopez}\` | surname order does not matter |
| \`Ana Muñoz Ruiz\` | \`ana {munoz, ruiz}\` | deliberate simplification: "ñ" folds to plain "n" |

## Captured phone number

| Submitted | Normalizes to | Why |
|---|---|---|
| \`+34 612 345 678\` | \`612345678\` | country code and spaces |
| \`0034612345678\` | \`612345678\` | international prefix |
| \`612-345-678\` | \`612345678\` | dashes stripped |

## Captured email

| Submitted | Normalizes to | Why |
|---|---|---|
| \`Ana.Garcia@Gmail.com\` | \`ana.garcia@gmail.com\` | case folded |
| \` ana.garcia @ gmail.com \` | \`ana.garcia@gmail.com\` | whitespace stripped |

## Appointment slot

| Submitted | Normalizes to | Why |
|---|---|---|
| \`2026-09-19T10:30:07+02:00\` | \`2026-09-19T10:30:00+02:00\` | seconds truncated |
| \`2026-09-19T08:30:00+00:00\` | \`2026-09-19T10:30:00+02:00\` | UTC converted to Europe/Madrid (CEST, UTC+2 in September) |

## Free-text enum values

| Submitted | Normalizes to | Why |
|---|---|---|
| \`  Review  \` | \`review\` | surrounding whitespace and case folded |
| \`NO_AVAILABILITY\` | \`no_availability\` | case folded |
| \`Paediátric_Review\` | \`paediatric_review\` | accents folded, same as names -- "á" -> "a" |
