# HackSpain — Prosper Track

## Challenge

[Prosper AI track](https://hackspain.app/tracks/prosper-ai)

## Team docs

- [PROSPER-TRACK.md](./PROSPER-TRACK.md) — guía de referencia del reto (API, scoring, problemas, setup)
- [ARCHITECTURE.md](./ARCHITECTURE.md) — cómo está construido y qué resuelve cada pieza
- [RUNBOOK.md](./RUNBOOK.md) — arrancar, exponer con ngrok, depurar, y la demo para el jurado

## Nuestra solución — El Turno

Un servidor WebSocket que habla Twilio Media Streams, con una consola en vivo
sobre el mismo puerto.

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python scripts\check_domain.py    # núcleo determinista
.\.venv\Scripts\python scripts\check_engine.py    # contra la clínica real
.\run.ps1
```

- Consola: <http://localhost:7860/>
- Endpoint de llamadas: `ws://localhost:7860/ws` (`wss://` a través de ngrok)

El principio de diseño: **el modelo conduce la conversación, el código decide el
registro.** Ningún id, minuto, tipo de cita, póliza ni motivo de rechazo sale de
lo que el modelo crea; todos salen de lo que devolvió la clínica. Está explicado
en [ARCHITECTURE.md](./ARCHITECTURE.md).

### Herramientas

| Script | Para qué |
| --- | --- |
| `scripts/check_domain.py` | Fechas, DNI, tipos de cita, cierres, triaje. Sin red, sin modelo |
| `scripts/check_engine.py` | El motor contra la clínica real: huecos, reglas, rechazos |
| `scripts/rehearse.py` | Escenarios con la forma de los problemas puntuados, en texto y sin cuota |
| `scripts/mock_call.py` | Marca nuestro propio socket, N a la vez. El chequeo del problema 2 |
| `scripts/fetch_catalog.py` | Cachea el catálogo de la clínica en `data/` |
| `scripts/smoke_test.py` | Que la clave y el host responden |

## What we're building

A voice AI agent that answers inbound scheduling calls for a clinic, the way a receptionist would.

Someone rings. Our agent picks up, works out who is calling and what they need, looks them up in the clinic's records, finds real availability, and books, moves or cancels the appointment. Some calls should not end in a booking at all — the clinic cannot do it, the caller needs a doctor now, the rules say no — and recognising those is as much a part of the job as booking well.

The voice model is one component of the system we design, not the system. Doing well means building around it: real lookups, real availability, checks before anything is written, state that survives a caller changing their mind, and enough visibility to explain why the agent said what it said.

## How the weekend runs

Friday 18 → Sunday 20 September.

1. **Register and stand up an endpoint.** Register the team, get a key, stand up an endpoint that can be called. The starter kit gets a talking agent running in minutes; everything after that is ours to build.
2. **Build and rehearse.** Dial ourselves as often as we like against published practice cases, answers included.
3. **Run for score** when ready. The organizers call the agent with every problem, check what it did, and points go on the leaderboard.
4. **Checkpoints.** Twice over the weekend the board freezes and prizes go to whoever is leading. Being early pays.
5. **Sunday: the final boss.** The jury calls the agent themselves, and we show them what we built.

## What the callers throw at us

Eighteen problems, each with its own persona calling in. Each one isolates a single thing that makes a real front desk hard, on top of the same ordinary booking:

- The straightforward booking, and ten of them at once.
- A caller the records do not know yet, and a caller who matches four people.
- Someone asking for a specific doctor, a specific site, or "the soonest".
- Vague times — "next Thursday", "first thing Monday" — that have to resolve.
- Requests the clinic's rules forbid, which must be refused for the right reason.
- A full diary with nothing free.
- Changes and cancellations.
- A parent calling for a child, a daughter for her father.
- Someone who should be sent to a doctor, not a calendar.
- Callers not speaking English, including other languages of Spain.
- A terrible line, a caller who interrupts and corrects and changes their mind, and someone trying to talk the agent into something it should not do.

## How we're scored

Two things, added together.

### The leaderboard

Automatic, and brutally literal. After each call the agent tells the graders what it did. Either that matches what the case accepts, or the case fails. There is no partial credit, no points for a nice conversation, and no credit for nearly. A call that correctly refuses still has to say so; silence is always wrong.

### The jury's final boss

Everything the leaderboard ignores. The jury calls us themselves and judges the call as a person on the phone would: how it sounds, how it handles being interrupted, whether it feels like the clinic knows who is calling. Then they judge what we built around it — how a call is orchestrated, what's visible while it's happening, what can be learned from it afterwards, and whether we can show any of it working. Safety, language, and how we know our own agent works all count.

## Sponsor credits available

We have free access to the following platforms if we need them (using them is optional, and we're free to use other tools too):

- **Vercel** — AI Gateway credits · $50
- **QuiverAI** — API credits · $50. AI-native design tool and research company.
- **Fal AI** — API credits · $50. Generative media platform / fast inference engine for image, video, audio, and 3D models.
- **Cloudflare** — AI Gateway · $100. Build, deploy, and govern AI agents on the same network — secure MCP portals, identity-aware access, built-in inference.
- **Exa** — API credits · $50. Search API for AI agents needing real-time web data, deep research, and structured content.
- **Cognition** — Devin Max plan · $200 in codes. Devin, the autonomous software engineer that plans, writes, tests, and ships code.
- **Cursor**
- **Helmcode** — 600M tokens (DeepSeek V4 / GLM 5.3). Managed AI inference infrastructure.
