"""Dry-run silent sockets so the floor can show concurrency. Never scored."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any

import websockets

from src.voice.audio import FRAME_BYTES, FRAME_MS, frames, silence


async def silent_call(url: str, index: int, seconds: float) -> dict[str, Any]:
    call_id = f"demo-{uuid.uuid4()}"
    stream_sid = f"MZ{uuid.uuid4().hex[:30]}"
    connected = False
    error = None
    try:
        async with websockets.connect(url, open_timeout=8, close_timeout=4) as socket:
            connected = True
            await socket.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
            await socket.send(json.dumps({
                "event": "start",
                "sequenceNumber": "1",
                "streamSid": stream_sid,
                "start": {
                    "streamSid": stream_sid,
                    "accountSid": f"AC{uuid.uuid4().hex[:30]}",
                    "callSid": call_id,
                    "tracks": ["inbound"],
                    "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                    "customParameters": {
                        "call_id": call_id,
                        "dry_run": "true",
                        "from_number": f"+346{20000000 + index:08d}"[:12],
                    },
                },
            }))
            sequence = 2
            tick = asyncio.get_event_loop().time()
            for chunk in frames(silence(int(seconds * 1000)), FRAME_BYTES):
                await socket.send(json.dumps({
                    "event": "media",
                    "sequenceNumber": str(sequence),
                    "streamSid": stream_sid,
                    "media": {
                        "track": "inbound",
                        "chunk": str(sequence),
                        "timestamp": str(int((sequence - 2) * FRAME_MS)),
                        "payload": base64.b64encode(chunk).decode("ascii"),
                    },
                }))
                sequence += 1
                tick += FRAME_MS / 1000
                delay = tick - asyncio.get_event_loop().time()
                if delay > 0:
                    await asyncio.sleep(delay)
            await socket.send(json.dumps({
                "event": "stop", "sequenceNumber": str(sequence), "streamSid": stream_sid,
                "stop": {"accountSid": f"AC{uuid.uuid4().hex[:30]}", "callSid": call_id},
            }))
            await asyncio.sleep(0.4)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {"call_id": call_id, "connected": connected, "error": error}


async def burst(url: str, count: int, seconds: float) -> list[dict[str, Any]]:
    return await asyncio.gather(*[silent_call(url, i, seconds) for i in range(count)])
