from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

from src.voice.audio import ulaw_to_wav


class RunTape:
    def __init__(self, root: Path, run_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            raise ValueError("unsafe run identifier")
        self.root, self.run_id = root, run_id
        self.started = time.monotonic()
        self.tracks = {"inbound": bytearray(), "outbound": bytearray()}
        self.frames = {"inbound": 0, "outbound": 0}
        self.signal_frames = 0
        self.first_signal_ms: int | None = None
        self.closed_at: float | None = None
        self.cap = 8000 * 180

    def append(self, track: str, payload: bytes, timestamp_ms: int | None = None):
        if track not in self.tracks or not payload:
            return
        self.frames[track] += 1
        if track == "outbound" and payload.translate(None, b"\xff\x7f"):
            self.signal_frames += 1
            if self.first_signal_ms is None:
                self.first_signal_ms = round((time.monotonic() - self.started) * 1000)
        offset = int((time.monotonic() - self.started) * 8000) if timestamp_ms is None else max(0, timestamp_ms * 8)
        buf = self.tracks[track]
        offset = min(self.cap, max(len(buf), offset))
        buf.extend(b"\xff" * (offset - len(buf)))
        buf.extend(payload[:max(0, self.cap - len(buf))])

    def save(self) -> dict:
        folder = self.root / "audio" / self.run_id
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in self.tracks.items():
            with (folder / f"{name}.wav").open("wb") as handle:
                handle.write(ulaw_to_wav(bytes(data)))
        return {"frames": self.frames, "outbound_non_silent_frames": self.signal_frames,
                "first_signal_ms": self.first_signal_ms,
                "audio_status": "signal_sent" if self.signal_frames else "silent",
                "observation": "socket_send_completed_not_playback_acknowledged"}


class RecordedSocket:
    def __init__(self, socket, tape: RunTape):
        self.socket, self.tape = socket, tape

    def __getattr__(self, name):
        return getattr(self.socket, name)

    async def receive(self):
        message = await self.socket.receive()
        if message["type"] == "websocket.disconnect":
            self.tape.closed_at = time.monotonic()
        if message.get("text"):
            value = json.loads(message["text"])
            if value.get("event") == "stop":
                self.tape.closed_at = time.monotonic()
            elif value.get("event") == "media":
                media = value.get("media", {})
                payload = base64.b64decode(media.get("payload", ""), validate=True)
                if len(payload) > 8000:
                    raise ValueError("oversized audio packet")
                timestamp = media.get("timestamp")
                self.tape.append("inbound", payload, int(timestamp) if timestamp is not None else None)
        return message

    async def send_text(self, text: str):
        await self.socket.send_text(text)
        value = json.loads(text)
        if value.get("event") == "media":
            self.tape.append("outbound", base64.b64decode(value["media"]["payload"], validate=True))
