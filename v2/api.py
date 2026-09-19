from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import json
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from v2.clinic import ClinicBoundary, Dispatcher, FixtureClinic
from v2.config import Config
from v2.domain.catalog import Catalog
from v2.evaluation import demo_request, metrics, rehearse
from v2.models import MAX_CALL_TURNS, CallState, Language, RehearsalRequest, Reply
from v2.platform_api.client import PlatformClient
from v2.providers import VercelInterpreter
from v2.store import BudgetExceeded, RunStore
from v2.workflow import CallController, TEXT


class SafePlatformClient(PlatformClient):
    async def submit(self, *args, **kwargs):
        raise PermissionError("practice reads cannot submit to the platform")


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Language = "en"
    mode: Literal["simulation", "practice"] = "simulation"


class TextTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)


@dataclass
class TextSession:
    controller: CallController
    clinic: object
    interpreter: VercelInterpreter
    touched: float
    busy: bool = False


@dataclass(frozen=True)
class VoiceTicket:
    call_id: str
    stream_sid: str
    language: Language
    mode: str
    origin: str
    expires_at: float


def create_app(config: Config | None = None, store: RunStore | None = None) -> FastAPI:
    config = config or Config()
    if config.mode == "live" and not (config.allow_submissions and config.release_approved):
        raise ValueError("live operation requires explicit submission and release approval after acceptance testing")

    async def close_text(run_id: str, status: str = "disconnected"):
        session = app.state.text_sessions.pop(run_id, None)
        if session is None:
            return
        state = session.controller.state
        if status == "disconnected" and state.completion_requested and state.all_resolved:
            status = "completed"
        app.state.store.event(run_id, "session_ended", {"status": status, "official_grade": None})
        app.state.store.save(state)
        await asyncio.gather(session.interpreter.close(), session.clinic.close(), return_exceptions=True)

    async def expire_sessions():
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            for run_id, session in list(app.state.text_sessions.items()):
                if not session.busy and now - session.touched >= config.text_session_ttl_s:
                    await close_text(run_id, "timed_out")
            app.state.voice_tickets = {key: ticket for key, ticket in app.state.voice_tickets.items()
                                       if ticket.expires_at > now}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from loguru import logger
        logger.remove()
        logger.add(sys.stderr, level="WARNING", diagnose=False, backtrace=False,
                   format=lambda _: "{time} | {level} | {name} | provider event (see run diagnostics)\n")
        app.state.store = store or RunStore(str(config.data_dir / "runs.db"))
        app.state.voice_slot = asyncio.Semaphore(config.max_voice_calls)
        app.state.voice_active = set()
        app.state.text_sessions = {}
        app.state.voice_tickets = {}
        # Importing the voice stack takes ~2 s; paid here, not while a caller's audio queues up.
        await asyncio.to_thread(importlib.import_module, "v2.voice")
        expiry = asyncio.create_task(expire_sessions())
        try:
            yield
        finally:
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)
            for run_id in list(app.state.text_sessions):
                await close_text(run_id)
            app.state.voice_tickets.clear()
            if store is None:
                app.state.store.close()

    app = FastAPI(title="Socket Wizard", version="2.0.0", lifespan=lifespan)
    app.mount("/web", StaticFiles(directory=Path(__file__).parent / "web"), name="web")

    @app.middleware("http")
    async def bounded_requests(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH"}:
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 65_536:
                    return JSONResponse({"detail": "request body exceeds 65536 bytes"}, status_code=413,
                                        headers={"Cache-Control": "no-store"})
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        if request.url.path.startswith("/api/") or request.url.path == "/health":
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "microphone=(self), camera=()"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self'; "
                                                      "connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
                                                      "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; "
                                                      "frame-ancestors 'none'; form-action 'self'")
        return response

    def valid_token(token: str | None) -> bool:
        return bool(config.operator_token and token and len(token) <= 4096
                    and hmac.compare_digest(config.operator_token.encode(), token.encode()))

    def authenticated(x_v2_token: str | None = Header(default=None)):
        if not valid_token(x_v2_token):
            raise HTTPException(401, "operator token required")

    def require_paid(*, voice: bool = False, mode: str = "simulation"):
        missing = config.missing_voice() if voice else config.missing_text()
        if mode != "simulation" and not config.api_key:
            missing = [*missing, "PLATFORM_API_KEY"]
        if missing:
            raise HTTPException(503, {"message": "providers are not configured", "missing": missing})
        minimum = 3_100_000 if voice else 100_000
        if app.state.store.budget()["remaining_microusd"] < minimum:
            raise HTTPException(402, "insufficient unreserved API budget")

    def report_run(run_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            raise HTTPException(404, "unknown run")
        report = app.state.store.report(run_id)
        if report is None:
            raise HTTPException(404, "unknown run")
        return {**report, "metrics": metrics(report)}

    async def controller_for(call_id: str, language: Language, mode: str, transport: str):
        state = CallState(call_id=call_id, mode=mode, language=language,
                          reference_time=datetime.now(ZoneInfo("Europe/Madrid")))
        async def clinic_event(kind, payload):
            app.state.store.event(state.run_id, kind, {
                "path": payload.get("path"), "status": payload.get("status"),
                "elapsed_ms": payload.get("elapsed_ms"), "failed": bool(payload.get("error")),
            })
        client_type = PlatformClient if mode == "live" else SafePlatformClient
        clinic = (FixtureClinic() if mode == "simulation" else
                  ClinicBoundary(client_type(on_event=clinic_event, config=config), Catalog.load(config.catalog_path)))
        interpreter = VercelInterpreter(config, app.state.store)
        try:
            dispatcher = Dispatcher(app.state.store, mode,
                                    sender=clinic.client.submit if mode == "live" else None,
                                    allow_live=config.allow_submissions and config.release_approved)
            controller = CallController(state, clinic, app.state.store, dispatcher, interpreter,
                                        {**config.manifest(), "transport": transport})
        except Exception:
            await asyncio.gather(interpreter.close(), clinic.close(), return_exceptions=True)
            raise
        return controller, clinic, interpreter

    @app.exception_handler(BudgetExceeded)
    async def exhausted(_request, _exc):
        return JSONResponse({"detail": "authorized API budget exhausted"}, status_code=402)

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": config.mode, "live_cutover_enabled": config.mode == "live",
                "voice_ready": not config.missing_voice(), "text_ready": not config.missing_text(),
                "clinic_ready": bool(config.api_key), "assessment_ready": bool(config.allow_paid and config.jev_url and config.jev_token),
                "missing": config.missing_voice(), "readiness_source": "configuration_only",
                "languages": ["es", "en", "ca"], "voice_clone": "not_enabled",
                "active_voice_calls": len(app.state.voice_active), "max_voice_calls": config.max_voice_calls}

    @app.get("/")
    async def console():
        return FileResponse(Path(__file__).parent / "console.html")

    @app.get("/api/budget", dependencies=[Depends(authenticated)])
    async def budget():
        return app.state.store.budget()

    @app.get("/api/demo", dependencies=[Depends(authenticated)])
    async def demo(language: Language = "en"):
        return demo_request(language).model_dump()

    @app.post("/api/rehearse", dependencies=[Depends(authenticated)])
    async def run_rehearsal(request: RehearsalRequest):
        return await rehearse(request, app.state.store, config.manifest())

    @app.get("/api/runs", dependencies=[Depends(authenticated)])
    async def runs(limit: int = Query(50, ge=1, le=100), before: str | None = Query(None, max_length=512)):
        try:
            return app.state.store.list_runs(limit=limit, before=before)
        except ValueError:
            raise HTTPException(400, "invalid run cursor") from None

    @app.get("/api/runs/{run_id}", dependencies=[Depends(authenticated)])
    async def run_detail(run_id: str):
        return report_run(run_id)

    @app.get("/api/runs/{run_id}/audio/{track}", dependencies=[Depends(authenticated)])
    async def recording(run_id: str, track: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id) or track not in {"inbound", "outbound"}:
            raise HTTPException(404, "unknown recording")
        report_run(run_id)
        path = config.data_dir / "audio" / run_id / f"{track}.wav"
        if not path.is_file():
            raise HTTPException(404, "no recording")
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions/text", dependencies=[Depends(authenticated)])
    async def text_session(request: SessionRequest):
        require_paid(mode=request.mode)
        if len(app.state.text_sessions) >= config.max_text_sessions:
            raise HTTPException(429, "text session capacity reached; close an existing session")
        controller, clinic, interpreter = await controller_for("text-" + uuid4().hex, request.language, request.mode, "text")
        state = controller.state
        session = TextSession(controller, clinic, interpreter, time.monotonic())
        app.state.text_sessions[state.run_id] = session
        app.state.store.event(state.run_id, "session_started", {"status": "active", "transport": "text"})
        reply = Reply(text=TEXT[state.language]["hello"], language=state.language, epoch=controller.epoch)
        controller.pending_reply = reply
        app.state.store.event(state.run_id, "response_planned", {**reply.model_dump(), "elapsed_ms": 0})
        controller.presented(reply)
        return {**report_run(state.run_id), "reply": reply.model_dump()}

    @app.post("/api/sessions/{run_id}/turn", dependencies=[Depends(authenticated)])
    async def text_turn(run_id: str, request: TextTurnRequest):
        session = app.state.text_sessions.get(run_id)
        if session is None:
            raise HTTPException(404, "unknown or expired text session")
        if session.busy:
            raise HTTPException(409, "a caller turn is still being processed")
        if not request.text.strip():
            raise HTTPException(422, "caller text must not be blank")
        if session.controller.state.turn >= MAX_CALL_TURNS:
            raise HTTPException(409, "session turn limit reached")
        require_paid(mode=session.controller.state.mode)
        session.busy = True
        try:
            reply = await asyncio.wait_for(session.controller.turn(request.text), timeout=60)
            if reply:
                session.controller.presented(reply)
            return {**report_run(run_id), "reply": reply.model_dump() if reply else None}
        except TimeoutError:
            session.controller.interrupt(source="timeout")
            app.state.store.event(run_id, "error", {"where": "text_turn", "error_type": "TimeoutError"})
            raise HTTPException(504, "conversation provider timed out") from None
        finally:
            session.touched = time.monotonic()
            session.busy = False

    @app.delete("/api/sessions/{run_id}", dependencies=[Depends(authenticated)])
    async def end_text(run_id: str):
        session = app.state.text_sessions.get(run_id)
        if session is None:
            raise HTTPException(404, "unknown or expired text session")
        if session.busy:
            raise HTTPException(409, "wait for the current turn before closing")
        await close_text(run_id)
        return report_run(run_id)

    def origin_allowed(origin: str, connection) -> bool:
        same_origin = str(connection.url).split("://", 1)[1].split("/", 1)[0]
        scheme = "https" if connection.url.scheme in {"https", "wss"} else "http"
        return origin == f"{scheme}://{same_origin}" or origin in config.allowed_origins

    @app.post("/api/voice/ticket", dependencies=[Depends(authenticated)])
    async def voice_ticket(fields: SessionRequest, request: Request):
        origin = request.headers.get("origin") or str(request.base_url).rstrip("/")
        if not origin_allowed(origin, request):
            raise HTTPException(403, "browser origin is not allowed")
        require_paid(voice=True, mode=fields.mode)
        now = time.monotonic()
        app.state.voice_tickets = {key: value for key, value in app.state.voice_tickets.items() if value.expires_at > now}
        if len(app.state.voice_tickets) >= 100 or app.state.voice_slot.locked():
            raise HTTPException(429, "voice capacity reached")
        token = secrets.token_urlsafe(32)
        ticket = VoiceTicket("browser-" + uuid4().hex, "MZ" + uuid4().hex, fields.language,
                             fields.mode, origin, now + config.ticket_ttl_s)
        app.state.voice_tickets[hashlib.sha256(token.encode()).hexdigest()] = ticket
        return {"ticket": token, "expires_in": config.ticket_ttl_s, "websocket_path": "/ws/browser",
                "call_id": ticket.call_id, "stream_sid": ticket.stream_sid}

    async def handshake(socket: WebSocket, ticket: VoiceTicket | None):
        async def message():
            text = await asyncio.wait_for(socket.receive_text(), 5)
            if len(text) > 8192:
                raise ValueError("oversized handshake")
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError("invalid handshake")
            return value
        start = await message()
        if start.get("event") == "connected":
            start = await message()
        if start.get("event") != "start" or not isinstance(start.get("start"), dict):
            raise ValueError("expected start handshake")
        fields = start["start"]
        media = fields.get("mediaFormat", {})
        if not isinstance(media, dict) or media.get("encoding") != "audio/x-mulaw" or media.get("sampleRate") != 8000 or type(media.get("channels")) is not int or media["channels"] != 1:
            raise ValueError("expected mono 8kHz mu-law")
        call_id, stream_sid = fields.get("callSid"), fields.get("streamSid") or start.get("streamSid")
        if not isinstance(call_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", call_id):
            raise ValueError("invalid call identifier")
        if not isinstance(stream_sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", stream_sid):
            raise ValueError("invalid stream identifier")
        if ticket and (call_id != ticket.call_id or stream_sid != ticket.stream_sid):
            raise ValueError("browser call identity does not match ticket")
        return call_id, stream_sid

    async def serve_voice(socket: WebSocket, ticket: VoiceTicket | None = None):
        mode = ticket.mode if ticket else config.mode
        try:
            require_paid(voice=True, mode=mode)
        except HTTPException:
            await socket.close(code=1013)
            return
        if app.state.voice_slot.locked():
            await socket.close(code=1013)
            return
        async with app.state.voice_slot:
            await socket.accept(subprotocol="v2-voice" if ticket else None)
            accepted_at = time.monotonic()
            controller = interpreter = clinic = None
            status = "disconnected"
            try:
                call_id, stream_sid = await handshake(socket, ticket)
                language = ticket.language if ticket else config.default_language
                controller, clinic, interpreter = await controller_for(call_id, language, mode, "browser" if ticket else "carrier")
                state = controller.state
                app.state.voice_active.add(state.run_id)
                app.state.store.event(state.run_id, "call_started", {"status": "active", "transport": "browser" if ticket else "carrier"})
                on_ready = None
                if ticket:
                    # The browser starts streaming on "ready", so it is sent once the pipeline reads audio.
                    async def on_ready(run_id=state.run_id):
                        await socket.send_json({"event": "ready", "run_id": run_id})
                from v2.voice import run_voice
                await run_voice(socket, stream_sid, controller, config, on_ready=on_ready, accepted_at=accepted_at)
                events = app.state.store.report(state.run_id)["events"]
                terminal = next((event["kind"] for event in reversed(events)
                                 if event["kind"] in {"completion_close", "deadline", "pipeline_error", "protocol_error"}), None)
                if terminal == "completion_close" and state.all_resolved:
                    status = "completed"
                elif terminal == "deadline":
                    status = "timed_out"
                elif terminal in {"pipeline_error", "protocol_error"}:
                    status = "error"
            except WebSocketDisconnect:
                pass
            except (ValueError, KeyError, TypeError):
                status = "error"
                if controller:
                    app.state.store.event(controller.state.run_id, "error", {"where": "socket", "type": "InvalidProtocol"})
                await close_socket(socket, 1008)
            except Exception as exc:
                status = "error"
                if controller:
                    app.state.store.event(controller.state.run_id, "error", {"where": "socket", "type": type(exc).__name__})
            finally:
                if controller:
                    app.state.voice_active.discard(controller.state.run_id)
                    app.state.store.event(controller.state.run_id, "call_ended", {"status": status, "official_grade": None})
                    app.state.store.save(controller.state)
                resources = [item.close() for item in (interpreter, clinic) if item is not None]
                if resources:
                    await asyncio.gather(*resources, return_exceptions=True)
                await close_socket(socket)

    async def close_socket(socket, code: int = 1000):
        try:
            await socket.close(code=code)
        except (RuntimeError, WebSocketDisconnect):
            pass

    @app.websocket("/ws")
    async def carrier_voice(socket: WebSocket):
        if socket.query_params or not valid_token(socket.headers.get("x-v2-token")):
            await socket.close(code=1008)
            return
        await serve_voice(socket)

    @app.websocket("/ws/browser")
    async def browser_voice(socket: WebSocket):
        protocols = socket.scope.get("subprotocols", [])
        origin = socket.headers.get("origin", "")
        if socket.query_params or len(protocols) != 2 or protocols[0] != "v2-voice" or not protocols[1].startswith("ticket.") or not origin_allowed(origin, socket):
            await socket.close(code=1008)
            return
        token = protocols[1][7:]
        key = hashlib.sha256(token.encode()).hexdigest()
        ticket = app.state.voice_tickets.get(key) if len(token) <= 128 else None
        if ticket is None or ticket.expires_at <= time.monotonic() or ticket.origin != origin:
            await socket.close(code=1008)
            return
        app.state.voice_tickets.pop(key)
        await serve_voice(socket, ticket)

    return app


app = create_app()
