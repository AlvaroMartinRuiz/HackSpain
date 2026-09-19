"""The socket the harness dials: Twilio's Media Streams format, no Twilio.

The handshake is camelCase and its numbers arrive as strings, which is the
part that bites teams. A fresh pipeline is built per connection.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.agent.llm import LLMClient
from src.domain.catalog import Catalog
from src.obs.store import store
from src.telephony.session import CallSession

router = APIRouter()

_catalog: Optional[Catalog] = None
_llm: Optional[LLMClient] = None


def configure(catalog: Catalog, llm: LLMClient) -> None:
    global _catalog, _llm
    _catalog, _llm = catalog, llm


@router.websocket("/ws")
async def media_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    session: Optional[CallSession] = None

    async def send(message: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(message))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except ValueError:
                continue

            event = message.get("event")

            if event == "connected":
                continue

            if event == "start":
                start = message.get("start") or {}
                parameters = start.get("customParameters") or {}
                call_id = start.get("callSid") or parameters.get("call_id")
                stream_sid = start.get("streamSid") or message.get("streamSid") or ""
                from_number = parameters.get("from_number") or None
                dry_run = str(parameters.get("dry_run") or "").strip().lower() in {
                    "1", "true", "yes", "on",
                }

                if not call_id:
                    await websocket.close(code=1008)
                    return

                store.open_call(call_id, from_number)
                await store.announce({"type": "call_started", "call_id": call_id})

                assert _catalog is not None and _llm is not None
                session = CallSession(
                    call_id=call_id,
                    stream_sid=stream_sid,
                    from_number=from_number,
                    send=send,
                    catalog=_catalog,
                    store=store,
                    llm=_llm,
                    dry_run=dry_run,
                    close=lambda: _close_quietly(websocket),
                )
                await session.record("call_started", {
                    "from_number": from_number,
                    "stream_sid": stream_sid,
                    "media_format": start.get("mediaFormat"),
                    "local_talk": dry_run,
                })
                await session.start()
                continue

            if event == "media" and session is not None:
                media = message.get("media") or {}
                payload = media.get("payload")
                if payload:
                    await session.on_media(payload, track=media.get("track"))
                continue

            if event == "stop" and session is not None:
                await session.on_stop()
                break

            if event in {"mark", "dtmf"} and session is not None:
                await session.record("wire", {"event": event, "body": message.get(event)})

    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:
        # A session deadline may close the socket while receive_text is
        # awaiting its next frame. That is an intentional clean shutdown.
        if session is not None and "not connected" not in str(exc).lower():
            await session.record("error", {
                "where": "socket", "detail": f"RuntimeError: {exc}",
            })
    except Exception as exc:
        if session is not None:
            await session.record("error", {"where": "socket", "detail": f"{type(exc).__name__}: {exc}"})
    finally:
        if session is not None:
            # The submission window stays open after the socket closes, so the
            # record is settled here rather than abandoned.
            await session.finalize()
        with_suppressed = asyncio.shield(_close_quietly(websocket))
        await with_suppressed


async def _close_quietly(websocket: WebSocket) -> None:
    try:
        await websocket.close()
    except Exception:
        pass
