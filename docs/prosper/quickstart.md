# Get on the phone

From nothing to an agent answering a judged call. Budget an hour, most of it
waiting on API keys.

You are building **a WebSocket server that answers the phone**. We call it,
play a patient who wants an appointment, and your agent POSTs back what it
would have booked. There is no phone number and no Twilio account on your
side — just a socket.

## 1. Collect your account and key

**Organisers open your account — you do not register yourself.** There is no
sign-up form and no event code to type. Come to the registration desk with a
team name and one email address, and the desk reads back three things on one
screen:

| What | Used for |
|---|---|
| Your **email** | Signing in to the dashboard |
| A generated **dashboard password** | The same sign-in. You do not choose it |
| Your **API key** (\`pk-…\`) | Every request your agent makes |

**Copy all three before that screen is gone.** The key and the password are
shown there and nowhere else — neither is stored anywhere it can be read back.

A lost key is rotated at the desk, and the old one stops working. A lost
password means the desk revokes the account and opens a new one. There is no
reset email, so an address with a typo is an account only the desk can undo.

Extra teammates who want their own dashboard login are added at the desk
against the same team. The **API key belongs to the team, not to a person**:
there is one, and every agent on the team uses it.

The desk also hands over a **prepaid debit card with €100 on it, one per team**.
That is your budget for whatever your agent runs on — models, speech, telephony,
tunnels. Spend it how you like. It is not topped up and there is nothing to
claim back afterwards, so treat the card as the whole allowance.

\`\`\`bash
export PLATFORM_API_KEY=pk-...
export PLATFORM_API_BASE_URL=https://<the API host the desk gives you>

curl -sS "$PLATFORM_API_BASE_URL/api/v1/health"
curl -sS -H "X-Api-Key: $PLATFORM_API_KEY" \
  "$PLATFORM_API_BASE_URL/api/v1/directory?name=Marta%20Ruiz"
\`\`\`

Every route but \`/api/v1/health\` and the schema itself needs
\`X-Api-Key\`. Missing, invalid and revoked keys all return
\`403 {"detail":"Invalid API key"}\`. Another team's call or run is
indistinguishable from one that does not exist (\`404\`), and a request body
never chooses which team you are.

Prefer a browser: the [API reference](api.md) is the live schema. Press
*Authorize*, paste the key, and every endpoint is one *Execute* away.

## 2. Build your WebSocket server

There is no starter kit — building the thing that answers the phone is part
of the challenge. Your server speaks the wire format in
[the call contract](contract.md): Twilio's Media Streams protocol over a
plain WebSocket, no Twilio account or phone number needed.

Recommended: [pipecat](https://www.pipecat.ai/) for the voice pipeline —
speech to text, a model, speech back out. It ships a Twilio Media Streams
serializer and transport, and its own examples include a starter Twilio bot.

## 3. Expose it — ngrok

We call you from the internet, so \`localhost\` is unreachable. **ngrok is the
recommended tunnel**; anything that forwards WebSockets works.

\`\`\`bash
ngrok http 7860
\`\`\`

Your endpoint is that host with the \`wss://\` scheme and your socket's path:
\`https://a1b2c3d4.ngrok-free.app\` → \`wss://a1b2c3d4.ngrok-free.app/ws\`. Check
it with \`wscat -c wss://.../ws\` before handing it over.

Four things that bite teams:

- **A free ngrok URL changes every restart.** With an account, claim a static
  domain: \`ngrok http --url=your-name.ngrok-free.app 7860\`.
- **Pick a European region.** Audio is real-time 20ms frames; a tunnel routed
  through another continent adds delay to every one of them.
- **\`https://\` is not the endpoint.** The scheme is \`wss://\` and the path is
  whatever your server routes. Forgetting the path is the commonest mistake.
- **Keep the tunnel up for the whole run.** A dropped connection is a failed
  case. A scored run is one socket; problem 2's largest burst still opens twenty.

## 4. Tell us where to call you

The desk does not ask for an endpoint; every team starts on a placeholder. Set
the real one yourself, on the dashboard's **Settings** page, under
**Integration** — this is not a trip back to the desk.

Two fields:

| Field | What goes in it |
| --- | --- |
| **Endpoint** | \`wss://a1b2c3d4.ngrok-free.app/ws\` — scheme and path included |
| **Headers** | Optional. Anything the harness should send when it dials you, one per line: \`Authorization: Bearer …\` |

Saving replaces the whole configuration, so clearing the headers means none.
Header values are write-only: the page lists the names back, never the values.
A header WebSocket owns (\`Host\`, \`Connection\`, \`Upgrade\`, \`Sec-WebSocket-*\`)
is rejected, as is an endpoint that is not \`wss://…\` or \`ws://…\`.

The change applies to your **next** run: a run snapshots its endpoint when it
is admitted, so one already queued is dialled where it was queued.

## 5. Call yourself

The **Problems page** is the loop. Every call starts from there, and there are
two buttons:

| Button | Where | What it does |
| --- | --- | --- |
| **Call** | Beside each published case on a problem's Statement | One practice call on that case. Scores nothing |
| **Scored run** | On the problem you pick | One private case of that problem. This is what the standings come from |

Until Saturday 19 September the scored button was **Run All** (every open
problem, ten sockets). Those results still count.

Each problem lists its public cases with their answers beside the button. For a
practice call the submissions tab gives you the transcript, the recording, and
which fields your record lost — never what they should have been. A run's page
shows its state and its per-case verdicts as they land, and cancelling one is
safe at any point.

Your records are also readable from your agent, for a health check or your own
tooling:

\`\`\`bash
curl -sS -H "X-Api-Key: $PLATFORM_API_KEY" \
  "$PLATFORM_API_BASE_URL/api/v1/submissions?limit=50"
\`\`\`

The problem ids behind the page are in [the problem set](problems.md).

**One queued or active run at a time**, in either lane; both buttons are
disabled while that slot is occupied. Two clocks on top of that: 30 seconds
between practice calls, and **12 minutes** after your last scored run
**finished** before the next may start. The page counts the wait down for you.
A scored run is one call, so the wait is the cooldown, not eighteen minutes of
dialling.

Practice is where the feedback is. A **scored** case tells you only whether it
passed, whose failure it was and a failure signal until the reveal on Monday,
so debug against published cases and spend scored runs on problems you can
already pass. Each problem pays your first four passed cases. See
[scoring](rules.md).

## 6. What to build first

The score is binary per case: the actions you submit match one the case
accepts, or the case fails. There is no credit for a good conversation — that
is what the jury looks at instead. In rough order of points per hour:

1. **Identification.** Ask for a second identifier and use \`/directory\`'s
   exact fields — an exact field that does not match excludes the patient.
2. **The complete national id, including the check letter.** One wrong
   character fails the case.
3. **Refusals.** Several problems must be refused, not booked, and the
   \`reason\` has to name the rule that bit. Read the restriction metadata off
   \`/availability\` rather than guessing.
4. **The exact minute, with a timezone offset**, in Europe/Madrid.
5. **The appointment type.** It follows the patient and the specialty, never
   the request, and some specialties carry their own pair.
6. **Ten concurrent calls.** Everything per-socket, nothing shared.
7. **Turn-taking.** Interruption handling is entirely yours.

## Where to go next

| You want | Read |
|---|---|
| The wire, and the JSON you POST | [The call contract](contract.md) |
| The clinic, its rules, and how to schedule well | [The clinic](clinic-api.md) |
| What passes a case | [Scoring](rules.md) |
| What the callers ask for | [The 18 problems](problems.md) |
| Every field of every endpoint | [API reference](api.md) |
