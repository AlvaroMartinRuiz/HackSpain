# Socket Wizard

AI front desk for Clínica Arenal. A patient rings; the agent listens, uses the chart, books against real availability, and can show why.

The model conducts the conversation. Code decides the record.

## What it does

Someone calls the clinic. Socket Wizard picks up on a Twilio Media Streams WebSocket, identifies the caller against the clinic directory, opens the chart, searches real slots, and submits book / reschedule / cancel / register / no_action / escalate. After a successful submit it can send a confirmation email with a calendar file and directions. Nothing on that path is invented by the language model: IDs, slots, providers, insurance and reasons come from tools and the clinic API.

## Key capabilities

- Natural call handling: barge-in, Pipecat Smart Turn, stale-reply protection
- Chart-first personalization: visit history, usual site, insurer, accessibility notes
- Clinic rules: insurance, referral, age, location, third-party callers
- Safety: red flags escalate; it does not practise medicine
- Languages: Spanish, English, Catalan, including mid-call code-switch
- Follow-up: email + `.ics` + maps after book/reschedule (cancel and register too)
- Floor: live calls, Patient Journey, Why this decision, Safety, replay, rehearsal

## Architecture

One FastAPI process.

- `/ws` — Twilio-compatible media stream, one `CallSession` per call
- `/` and `/ops` — operations console
- `/demo` — same console, jury-facing (example call, dry-run concurrency)
- `/console` — classic single-call debugger
- `/health` — liveness for deploy

Voice: Deepgram or ElevenLabs Scribe (STT), ElevenLabs Flash (TTS), Cloudflare-compatible gpt-4o (LLM). Domain engine in `src/domain`. Tools in `src/agent`. Observability in `src/obs`. Mail in `src/notify/email.py`.

See [ARCHITECTURE.md](./ARCHITECTURE.md).

## How a call works

1. Socket opens. Session isolated from every other call.
2. Greeting. STT + Smart Turn wait until the caller has actually finished.
3. Tools look up the patient and the chart. The agent does not ask what the file already knows.
4. Availability and rules come from the clinic. The model may only talk about what tools returned.
5. Submit the record. Then, and only then, compose follow-up mail. Mail cannot fail the submit.

## Safety by design

- No medical advice. Red flags stop booking and escalate.
- Third-party callers do not get another patient's protected fields read back.
- National id and phone stay off the line unless the caller said them.
- Named-doctor requests warn and wait; the agent does not invent a substitute.

## Languages

ES / EN / CA. Detection can change mid-call without restarting the socket. Native ElevenLabs voices for Spanish and English; Catalan uses the conversational model once detected.

## Observability

The floor shows live activity (listening / thinking / speaking), a Patient Journey derived from the same events as the transcript, deterministic explainability, safety shields, post-call summary, reliability metrics, and replay. An **accepted record** is HTTP 200, not a scored leaderboard pass.

## Evaluation

```bash
python scripts/check_domain.py      # dates, DNI, types, triage — no network
python scripts/check_engine.py      # domain engine against the clinic
python scripts/check_product.py     # journey, why, safety, ICS
python scripts/rehearse.py          # text calls with no audio quota
python scripts/mock_call.py         # N simultaneous sockets against our /ws
```

## Run locally

```bash
python -m pip install -r requirements.txt
cp .env.example .env   # fill keys locally; never commit .env
python scripts/check_domain.py
python -m uvicorn src.main:app --host 127.0.0.1 --port 7860
```

- Console: http://localhost:7860/
- Demo: http://localhost:7860/demo
- Calls: `ws://localhost:7860/ws`

Full operator notes: [RUNBOOK.md](./RUNBOOK.md).

## Deploy

Long-lived process, not serverless. [DEPLOY.md](./DEPLOY.md). Dockerfile included. Register `PUBLIC_WS_URL` (`wss://…/ws`) with the desk. Protect the console with `CONSOLE_TOKEN`.

## Live demo

[JURY_DEMO.md](./JURY_DEMO.md) — 5 minutes, live first.  
[DEMO_VIDEO.md](./DEMO_VIDEO.md) — ~2 minute story cut.

Dry-run Talk and **Demo concurrency** never post scored records.

## HackSpain / Prosper

Built for the Prosper AI track at HackSpain (“El Turno” is the challenge name, not the product). Scoring still wants the record exactly right. The jury also scores how the call sounds, whether the clinic seems to know the caller, the platform you can drive live, safety, language, and how you know the agent works.

Challenge notes live under [docs/prosper/](./docs/prosper/).
