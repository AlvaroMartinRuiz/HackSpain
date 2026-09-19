"""What the console reads, and the live feed it watches."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.agent.llm import LLMClient
from src.config import settings
from src.domain.catalog import Catalog
from src.obs.store import store
from src.obs import tape
from src.platform_api.client import PlatformClient
from src.voice.tts import active_provider_name

router = APIRouter(prefix="/api/console")

ASSET_DIR = Path(__file__).resolve().parent.parent / "web" / "static" / "assets"

_catalog: Optional[Catalog] = None
_llm: Optional[LLMClient] = None
_assets_cache: Optional[tuple[float, dict[str, Any]]] = None


def configure(catalog: Catalog, llm: LLMClient) -> None:
    global _catalog, _llm
    _catalog, _llm = catalog, llm


@router.get("/overview")
async def overview() -> dict[str, Any]:
    missing = settings.missing_voice_keys()
    return {
        "clinic": _catalog.clinic_name if _catalog else None,
        "endpoint": {
            "port": settings.port,
            "path": "/ws",
            "public_ws_url": settings.public_ws_url or None,
            "public_console_url": settings.public_console_url or None,
        },
        "providers": {
            "stt": settings.stt_provider if settings.deepgram_api_key else "whisper (fallback)",
            "stt_model": settings.deepgram_model,
            "llm": f"{settings.llm_provider}:{settings.llm_model}" if settings.llm_api_key else "not configured",
            "tts": active_provider_name(),
        },
        "ready": not missing,
        "missing_keys": missing,
        "stats": store.aggregate(),
        "live": [call.summary() for call in store.live_calls()],
        "recent": _recent_for_console(),
    }


def _recent_for_console() -> list[dict[str, Any]]:
    recent = [call.summary() for call in store.recent_calls()[:20]]
    seen = {row["call_id"] for row in recent}
    for row in store.history(20):
        if row["call_id"] not in seen:
            recent.append(row)
            seen.add(row["call_id"])
        if len(recent) >= 20:
            break
    return recent


@router.get("/assets")
async def design_assets() -> dict[str, Any]:
    """The Quiver-drawn icon set, inlined in one response.

    Inlined rather than linked as <img> so the icons inherit `currentColor` and
    change with the theme, and fetched in one request so a dashboard opening
    twenty icons does not open twenty connections. An empty reply is a normal
    answer: the dashboard draws CSS shapes instead.
    """
    global _assets_cache

    if not ASSET_DIR.is_dir():
        return {"icons": {}, "manifest": {}, "generated": False}

    # Windows does not bump a directory's mtime when a file inside it changes,
    # so the cache key is the newest file mtime rather than the folder's.
    stamp = max(
        (path.stat().st_mtime for path in ASSET_DIR.iterdir()),
        default=ASSET_DIR.stat().st_mtime,
    )
    if _assets_cache is not None and _assets_cache[0] == stamp:
        return _assets_cache[1]

    icons: dict[str, str] = {}
    for path in sorted(ASSET_DIR.glob("*.svg")):
        try:
            icons[path.stem] = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue

    manifest: dict[str, Any] = {}
    manifest_path = ASSET_DIR / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}

    payload = {
        "icons": icons,
        "generated": bool(icons),
        "manifest": {
            "model": manifest.get("model"),
            "credits_spent": manifest.get("credits_spent", 0),
            "animated": sorted(
                name for name, row in (manifest.get("generated") or {}).items()
                if row.get("animated")
            ),
        },
    }
    _assets_cache = (stamp, payload)
    return payload


@router.get("/calls/{call_id}")
async def call_detail(call_id: str) -> dict[str, Any]:
    recordings = tape.available(call_id)
    call = store.load(call_id)
    if call is not None:
        return {**call.detail(), "recordings": recordings}
    events = store.replay(call_id)
    if not events:
        raise HTTPException(status_code=404, detail="no such call")
    return {"call_id": call_id, "replay_only": True, "events": events, "recordings": recordings}


@router.get("/calls/{call_id}/audio/{track}")
async def call_audio(call_id: str, track: str) -> FileResponse:
    path = tape.wav_path(call_id, track)
    if path is None:
        raise HTTPException(status_code=404, detail="no recording")
    return FileResponse(path, media_type="audio/wav", filename=f"{call_id}-{track}.wav")


@router.get("/calls/{call_id}/replay")
async def call_replay(call_id: str) -> dict[str, Any]:
    events = store.replay(call_id)
    if not events:
        raise HTTPException(status_code=404, detail="no such call")
    return {"call_id": call_id, "events": events}


@router.get("/history")
async def history(limit: int = 50) -> dict[str, Any]:
    return {"calls": store.history(limit)}


@router.get("/clinic")
async def clinic_records() -> dict[str, Any]:
    if _catalog is None:
        raise HTTPException(status_code=503, detail="catalogue not loaded")
    return _catalog.summary()


@router.get("/submissions")
async def platform_submissions(limit: int = 25) -> dict[str, Any]:
    client = PlatformClient()
    try:
        return {"submissions": await client.submissions(limit)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.aclose()


class RehearsalRequest(BaseModel):
    turns: list[str] = Field(..., description="What the caller says, one turn at a time.")
    from_number: Optional[str] = Field(default=None, description="Caller id, or none to withhold it.")
    dry_run: bool = Field(default=True, description="Keep the record local instead of posting it.")
    label: Optional[str] = None


@router.post("/rehearse")
async def rehearse(request: RehearsalRequest) -> dict[str, Any]:
    """Run a whole call as text: same brain, same tools, no audio and no quota.

    This is the loop the agent is developed in. A practice call costs thirty
    seconds of cooldown; this costs nothing and answers the same question —
    did the right record come out.
    """
    if _catalog is None or _llm is None:
        raise HTTPException(status_code=503, detail="not ready")

    from src.telephony.session import CallSession

    call_id = f"rehearsal-{uuid.uuid4()}"
    store.open_call(call_id, request.from_number)
    await store.announce({"type": "call_started", "call_id": call_id})

    async def sink(_message: dict[str, Any]) -> None:
        return None

    session = CallSession(
        call_id=call_id,
        stream_sid=call_id,
        from_number=request.from_number,
        send=sink,
        catalog=_catalog,
        store=store,
        llm=_llm,
        text_mode=True,
        dry_run=request.dry_run,
    )

    await session.record("call_started", {"rehearsal": True, "label": request.label,
                                          "from_number": request.from_number})
    try:
        await session.start()
        for turn in request.turns:
            await session.feed_text(turn)
    finally:
        # A rehearsal that throws half way through would otherwise sit in the
        # console as live for ever, and the concurrency figures read off it —
        # the ones the jury is shown — would be wrong from then on.
        await session.finalize(status="rehearsed")

    call = store.get(call_id)
    detail = call.detail() if call else {}
    return {
        "call_id": call_id,
        "label": request.label,
        "transcript": detail.get("transcript", []),
        "decisions": detail.get("decisions", []),
        "tool_calls": detail.get("tool_calls", []),
        "clinic_calls": detail.get("clinic_calls", []),
        "submissions": [
            {"action": s["action"], "payload": s["payload"], "status": s.get("status")}
            for s in detail.get("submissions", [])
        ],
        "errors": detail.get("errors", []),
        "metrics": detail.get("metrics", {}),
    }


@router.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = store.subscribe()
    try:
        await websocket.send_json({"type": "hello", "overview": await overview()})
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping", "stats": store.aggregate()})
                continue
            await websocket.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        store.unsubscribe(queue)
