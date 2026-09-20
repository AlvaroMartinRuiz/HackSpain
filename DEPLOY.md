# Deploy Socket Wizard

The product is one FastAPI process: HTTP dashboard + persistent WebSocket `/ws`.
Not serverless. Use Railway, Render or Fly.io.

## What you have to do (the part a laptop cannot finish)

I can push the image recipe. You still have to log into the host and into Prosper.

### 1. Push is already the deploy trigger

Branch: `pipecat-smart-turn`.
Repo: `AlvaroMartinRuiz/HackSpain`.
Root `Dockerfile` + `railway.toml`.

### 2. Railway (5 minutes)

1. Open https://railway.com and sign in with GitHub (`AlvaroMartinRuiz` or a collaborator).
2. **New project → Deploy from GitHub repo** → `HackSpain`.
3. Service settings: branch **`pipecat-smart-turn`**. Railway will see the Dockerfile.
4. **Variables** — paste from your local `.env`, then override these four:

```
HOST=0.0.0.0
RELOAD=false
PUBLIC_WS_URL=          # fill after step 5
PUBLIC_CONSOLE_URL=     # fill after step 5
```

Copy at least:

```
PLATFORM_API_KEY
PLATFORM_API_BASE_URL
CONSOLE_TOKEN
LLM_API_KEY
LLM_BASE_URL
LLM_MODEL
LLM_EXTRA_HEADERS
LLM_TIMEOUT_S
STT_PROVIDER
DEEPGRAM_API_KEY
ELEVENLABS_API_KEY
ELEVENLABS_VOICE_ID_EN
ELEVENLABS_VOICE_ID_ES
TTS_PROVIDER
AGENT_GREETING
DEFAULT_LANGUAGE
SMART_TURN
FOLLOWUP_EMAIL
```

Do **not** upload the `.env` file to GitHub. Paste in the Railway Variables UI.

5. **Settings → Networking → Generate domain.**
   Then set:

```
PUBLIC_WS_URL=wss://<that-domain>/ws
PUBLIC_CONSOLE_URL=https://<that-domain>/
```

   Redeploy once so those two stick.

6. Health: `https://<that-domain>/health` must say `"status":"ok"`.
   First boot can take 1–2 minutes (Smart Turn + TTS cache).

7. Jury / teammates:

```
https://<that-domain>/demo?token=<CONSOLE_TOKEN>
```

   Same `CONSOLE_TOKEN` as in Railway. `/ws` and `/health` do not need it.

### 3. Tell Prosper where to call you

Dashboard → **Settings → Integration**:

| Field | Value |
| --- | --- |
| Endpoint | `wss://<that-domain>/ws` |
| Headers | leave empty |

Scheme `wss://`, path `/ws`. Saving replaces the whole config. It applies to the **next** run, not one already queued.

### 4. Optional, but good for the demo

- `RESEND_API_KEY` + `FOLLOWUP_COPY` so a juror inbox gets the confirmation email.
- A dry-run Talk call on `/demo` before they walk in.
- Do **not** restart the service during a live jury call.

### Fallback if Railway is slow

Keep the laptop process + ngrok (European region, static domain if you have one). Register that `wss://…/ws` instead. Same console token.

---

## Platform notes

- Long-lived WebSockets (`/ws`, `/api/console/stream`)
- Outbound HTTPS (clinic, STT, TTS, LLM, Resend)
- Several `CallSession`s at once
- Env vars never committed

Never use Vercel serverless for the call path.

## Checks after deploy

- `GET /health` → `"status": "ok"`
- `GET /demo?token=…` loads the floor
- A dry-run Talk call appears as live
- Prosper practice **Call** reaches `/ws`
