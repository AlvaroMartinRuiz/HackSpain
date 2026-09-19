from __future__ import annotations

import asyncio
import hmac
import json
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import FileResponse

from v2.domain.catalog import Catalog
from v2.platform_api.client import PlatformClient
from v2.clinic import ClinicBoundary, Dispatcher, FixtureClinic
from v2.config import Config
from v2.evaluation import demo_request, metrics, rehearse
from v2.models import CallState, Language, RehearsalRequest
from v2.providers import VercelInterpreter
from v2.store import RunStore
from v2.workflow import CallController


class SafePlatformClient(PlatformClient):
    async def submit(self, *args, **kwargs):
        raise PermissionError("v2 practice reads cannot submit to the platform")


def create_app(config: Config | None = None, store: RunStore | None = None) -> FastAPI:
    config = config or Config()
    if config.mode == "live":
        raise ValueError("v2 live cutover is locked until provider/carrier acceptance tests and explicit release approval")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from loguru import logger
        logger.remove()
        logger.add(sys.stderr, level="WARNING", diagnose=False, backtrace=False,
                   format=lambda _: "{time} | {level} | {name} | provider event (see run diagnostics)\n")
        app.state.store = store or RunStore(str(config.data_dir / "runs.db"))
        app.state.voice_slot = asyncio.Semaphore(1)
        try:
            yield
        finally:
            if store is None:
                app.state.store.close()

    app = FastAPI(title="Socket Wizard v2 laboratory", lifespan=lifespan)

    @app.middleware("http")
    async def no_store(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def authenticated(x_v2_token: str | None = Header(default=None)):
        if not config.operator_token or not x_v2_token or not hmac.compare_digest(
            config.operator_token.encode(), x_v2_token.encode()
        ):
            raise HTTPException(401, "operator token required")

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": config.mode, "live_cutover_enabled": False,
                "voice_ready": not config.missing_voice(), "missing": config.missing_voice(),
                "languages": ["es", "en", "ca"], "voice_clone": "awaiting_recording"}

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

    @app.get("/api/runs/{run_id}", dependencies=[Depends(authenticated)])
    async def run_detail(run_id: str):
        report = app.state.store.report(run_id)
        if report is None:
            raise HTTPException(404, "unknown run")
        return {**report, "metrics": metrics(report)}

    @app.get("/api/runs/{run_id}/audio/{track}", dependencies=[Depends(authenticated)])
    async def recording(run_id: str, track: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id) or track not in {"inbound", "outbound"}:
            raise HTTPException(404, "unknown recording")
        if app.state.store.report(run_id) is None:
            raise HTTPException(404, "unknown run")
        path = config.data_dir / "audio" / run_id / f"{track}.wav"
        if not path.is_file():
            raise HTTPException(404, "no recording")
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def voice(socket: WebSocket):
        token = socket.headers.get("x-v2-token", "")
        if not config.operator_token or not hmac.compare_digest(token.encode(), config.operator_token.encode()):
            await socket.close(code=1008)
            return
        if config.missing_voice() or app.state.voice_slot.locked() or app.state.store.budget()["remaining_microusd"] < 3_100_000:
            await socket.close(code=1013)
            return
        async with app.state.voice_slot:
            await socket.accept()
            controller = interpreter = clinic = None
            try:
                first = json.loads(await asyncio.wait_for(socket.receive_text(), 5))
                start = first if first.get("event") == "start" else json.loads(await asyncio.wait_for(socket.receive_text(), 5))
                if start.get("event") != "start":
                    raise ValueError("expected start handshake")
                fields = start["start"]
                media = fields.get("mediaFormat", {})
                if media.get("encoding") != "audio/x-mulaw" or media.get("sampleRate") != 8000 or media.get("channels") != 1:
                    raise ValueError("expected mono 8kHz mu-law")
                stream_sid = fields.get("streamSid") or start.get("streamSid")
                if not isinstance(stream_sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", stream_sid):
                    raise ValueError("missing stream identifier")
                state = CallState(call_id=fields["callSid"], mode=config.mode, language="es",
                                  reference_time=datetime.now(ZoneInfo("Europe/Madrid")))
                async def clinic_event(kind, payload):
                    app.state.store.event(state.run_id, kind, {
                        "path": payload.get("path"), "status": payload.get("status"),
                        "elapsed_ms": payload.get("elapsed_ms"), "failed": bool(payload.get("error")),
                    })
                clinic = (ClinicBoundary(SafePlatformClient(on_event=clinic_event), Catalog.load())
                          if config.mode == "practice" else FixtureClinic())
                interpreter = VercelInterpreter(config, app.state.store)
                controller = CallController(state, clinic, app.state.store, Dispatcher(app.state.store, config.mode),
                                            interpreter, config.manifest())
                from v2.voice import run_voice
                await run_voice(socket, stream_sid, controller, config)
            except Exception as exc:
                if controller:
                    app.state.store.event(controller.state.run_id, "error", {"where": "socket", "type": type(exc).__name__})
            finally:
                if controller:
                    app.state.store.event(controller.state.run_id, "call_ended", {"official_grade": None})
                    app.state.store.save(controller.state)
                if interpreter:
                    await interpreter.close()
                if clinic:
                    await clinic.close()
                try:
                    await socket.close()
                except RuntimeError:
                    pass

    return app


app = create_app()
