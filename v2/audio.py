from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from pathlib import Path

from v2.codecs import ulaw_to_wav


class MediaProtocolError(ValueError):
    pass


def _integer(value, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MediaProtocolError(f"invalid {field}")
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,12}", text) or int(text) > maximum:
        raise MediaProtocolError(f"invalid {field}")
    return int(text)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MediaProtocolError("duplicate message field")
        result[key] = value
    return result


def parse_message(data: str | bytes, stream_sid: str | None = None) -> tuple[dict, bytes | None]:
    if not isinstance(data, (str, bytes)) or not 0 < len(data) <= 16384:
        raise MediaProtocolError("invalid message size")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise MediaProtocolError("invalid message JSON") from None
    if (not isinstance(value, dict) or not isinstance(value.get("event"), str)
            or value["event"] not in {"media", "mark", "stop", "dtmf", "clear"}):
        raise MediaProtocolError("unexpected stream event")
    sid = value.get("streamSid")
    if stream_sid is not None and (sid is not None or value["event"] in {"media", "mark"}) and sid != stream_sid:
        raise MediaProtocolError("stream identifier mismatch")
    if "sequenceNumber" in value:
        _integer(value["sequenceNumber"], "sequence number", 2**32 - 1)
    payload = None
    if value["event"] == "media":
        media = value.get("media")
        if not isinstance(media, dict) or media.get("track", "inbound") != "inbound":
            raise MediaProtocolError("invalid media track")
        encoded = media.get("payload")
        if not isinstance(encoded, str) or not 0 < len(encoded) <= 10668:
            raise MediaProtocolError("invalid audio payload size")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise MediaProtocolError("invalid base64 audio") from None
        if not 0 < len(payload) <= 8000:
            raise MediaProtocolError("invalid audio packet size")
        if "timestamp" in media:
            _integer(media["timestamp"], "media timestamp", 86_400_000)
        if "chunk" in media:
            _integer(media["chunk"], "media chunk", 2**32 - 1)
    elif value["event"] == "mark":
        mark = value.get("mark")
        if not isinstance(mark, dict) or not isinstance(mark.get("name"), str) or not 0 < len(mark["name"]) <= 128:
            raise MediaProtocolError("invalid stream mark")
    elif value["event"] == "dtmf":
        dtmf = value.get("dtmf")
        if not isinstance(dtmf, dict) or dtmf.get("digit") not in tuple("0123456789*#ABCD"):
            raise MediaProtocolError("invalid keypad event")
    return value, payload


class RunTape:
    def __init__(self, root: Path, run_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            raise ValueError("unsafe run identifier")
        self.root, self.run_id = root, run_id
        self.started = time.monotonic()
        self.tracks = {"inbound": bytearray(), "outbound": bytearray()}
        self.frames = {"inbound": 0, "outbound": 0}
        self.signal_frames = 0
        self.protocol_errors = 0
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

    def save(self, *, finalized: bool = False) -> dict:
        folder = self.root / "audio" / self.run_id
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in self.tracks.items():
            with (folder / f"{name}.wav").open("wb") as handle:
                handle.write(ulaw_to_wav(bytes(data)))
        return {"frames": dict(self.frames), "outbound_non_silent_frames": self.signal_frames,
                "first_signal_ms": self.first_signal_ms, "finalized": finalized,
                "audio_status": "signal_sent" if self.signal_frames else "silent",
                "observation": "socket_send_completed_not_playback_acknowledged"}


class RecordedSocket:
    def __init__(self, socket, tape: RunTape, *, stream_sid: str | None = None, send_timeout_s: float = 5,
                 accepted_at: float | None = None):
        self.socket, self.tape = socket, tape
        self.stream_sid, self.send_timeout_s = stream_sid, send_timeout_s
        self._send_lock = asyncio.Lock()
        # Audio that queued while the call was set up (providers connecting, models loading)
        # arrived in real time, so at the first read it is allowed once on top of the 2 s burst;
        # after that the allowance never refills past 2 s.
        self._accepted_at = accepted_at if accepted_at is not None else time.monotonic()
        self._audio_budget = 16000.0
        self._audio_budget_at: float | None = None

    def __getattr__(self, name):
        return getattr(self.socket, name)

    async def receive(self):
        message = await self.socket.receive()
        if message["type"] == "websocket.disconnect":
            self.tape.closed_at = time.monotonic()
            return message
        data = message.get("text") if message.get("text") is not None else message.get("bytes")
        try:
            value, payload = parse_message(data, self.stream_sid)
            if value["event"] == "clear":
                raise MediaProtocolError("unexpected inbound clear")
            if payload is not None:
                now = time.monotonic()
                if self._audio_budget_at is None:
                    self._audio_budget += max(0.0, now - self._accepted_at) * 8000
                else:
                    refilled = self._audio_budget + (now - self._audio_budget_at) * 8000
                    self._audio_budget = min(max(16000.0, self._audio_budget), refilled)
                self._audio_budget_at = now
                if len(payload) > self._audio_budget:
                    raise MediaProtocolError("inbound audio exceeds realtime budget")
                self._audio_budget -= len(payload)
        except MediaProtocolError:
            self.tape.protocol_errors += 1
            raise
        if value["event"] == "stop":
            self.tape.closed_at = time.monotonic()
        elif payload is not None:
            timestamp = value["media"].get("timestamp")
            self.tape.append("inbound", payload, int(timestamp) if timestamp is not None else None)
        return message

    async def send_text(self, text: str):
        await self.send_text_if_current(text, lambda: True)

    async def send_text_if_current(self, text: str, is_current) -> bool:
        _, payload = parse_message(text, self.stream_sid)
        async def send():
            async with self._send_lock:
                if not is_current():
                    return False
                await self.socket.send_text(text)
                if payload is not None:
                    self.tape.append("outbound", payload)
                return True
        return await asyncio.wait_for(send(), timeout=self.send_timeout_s)
