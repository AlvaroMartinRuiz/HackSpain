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
from src.obs.product import overlay_from_events
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
            "stt": settings.stt_active,
            "stt_model": settings.stt_model_label,
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
    # History-only calls are not still in memory, so rebuild the views the
    # console needs from the event log — especially the transcript.
    return {
        "call_id": call_id,
        "replay_only": True,
        "events": events,
        "recordings": recordings,
        **_detail_from_events(events),
    }


def _detail_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconstruct transcript / decisions / tools from a durable event log."""
    transcript: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    clinic_calls: list[dict[str, Any]] = []
    submissions: list[dict[str, Any]] = []
    followup_emails: list[dict[str, Any]] = []
    patient_full: Optional[dict[str, Any]] = None
    for event in events:
        kind = event.get("kind")
        payload = event.get("payload") or {}
        ts = event.get("ts")
        if kind == "stt_final":
            transcript.append({"role": "caller", "text": payload.get("text", ""), "ts": ts})
        elif kind == "agent_said":
            transcript.append({"role": "agent", "text": payload.get("text", ""), "ts": ts})
        elif kind == "interruption":
            transcript.append({
                "role": "caller", "cut": True, "ts": ts,
                "text": f"⟨cuts the agent⟩ {payload.get('heard') or ''}".strip(),
            })
        elif kind == "decision":
            decisions.append({**payload, "ts": ts})
        elif kind == "tool_call":
            tool_calls.append(payload)
        elif kind == "clinic_call":
            clinic_calls.append(payload)
        elif kind == "submit":
            submissions.append(payload)
        elif kind == "followup_email":
            followup_emails.append(payload)
        elif kind == "patient_identified":
            patient_full = payload.get("patient")
    actions = [
        {"action": s.get("action"), "accepted": s.get("accepted"), "status": s.get("status")}
        for s in submissions
    ]
    return {
        "transcript": transcript,
        "decisions": decisions,
        "tool_calls": tool_calls,
        "clinic_calls": clinic_calls,
        "submissions": submissions,
        "followup_emails": followup_emails,
        "actions": actions,
        "patient_full": patient_full,
        "turns": sum(1 for t in transcript if t.get("role") == "agent"),
        "product": overlay_from_events(events),
    }


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


@router.get("/reliability")
async def reliability() -> dict[str, Any]:
    from src.obs.product import compact_reliability
    stats = store.aggregate()
    return compact_reliability(stats, extras=stats)


@router.get("/demo/story")
async def demo_story() -> dict[str, Any]:
    from src.obs.demo_story import story
    return story()


class BurstRequest(BaseModel):
    calls: int = Field(default=6, ge=1, le=12)
    seconds: float = Field(default=8.0, ge=2.0, le=12.0)


_burst_lock = asyncio.Lock()


@router.post("/demo/concurrency")
async def demo_concurrency(request: BurstRequest) -> dict[str, Any]:
    """Silent dry-run sockets on this process. Never posts a scored record."""
    if _burst_lock.locked():
        raise HTTPException(status_code=409, detail="a concurrency demo is already running")

    async def run() -> None:
        async with _burst_lock:
            from src.obs.burst import burst
            url = f"ws://127.0.0.1:{settings.port}/ws"
            await burst(url, request.calls, request.seconds)

    asyncio.create_task(run(), name="demo-concurrency")
    return {"started": True, "calls": request.calls, "seconds": request.seconds, "dry_run": True}


@router.get("/calls/{call_id}/followup")
async def call_followup(call_id: str) -> dict[str, Any]:
    """HTML already composed by src.notify.email and saved under data/emails/."""
    call = store.load(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="no such call")
    emails = list(getattr(call, "followup_emails", None) or [])
    if not emails:
        raise HTTPException(status_code=404, detail="no follow-up on this call")
    last = emails[-1]
    html = ""
    path = last.get("path")
    if path:
        try:
            html = Path(path).read_text(encoding="utf-8")
        except OSError:
            html = ""
    ics_path = _ics_path(call_id)
    return {
        "subject": last.get("subject"),
        "to": last.get("to"),
        "html": html,
        "text": last.get("text"),
        "sent": last.get("sent"),
        "reason": last.get("reason"),
        "path": path,
        "action": last.get("action"),
        "maps_url": last.get("maps_url") or "",
        "ics": bool(last.get("ics") or ics_path),
        "ics_url": f"/api/console/calls/{call_id}/ics" if ics_path else None,
    }


def _ics_path(call_id: str) -> Optional[Path]:
    from src.notify.email import OUTBOX
    matches = sorted(OUTBOX.glob(f"{call_id}-*.ics"), reverse=True)
    return matches[0] if matches else None


@router.get("/calls/{call_id}/ics")
async def call_ics(call_id: str) -> FileResponse:
    path = _ics_path(call_id)
    if path is None:
        raise HTTPException(status_code=404, detail="no calendar file for this call")
    return FileResponse(path, media_type="text/calendar", filename=path.name)


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
