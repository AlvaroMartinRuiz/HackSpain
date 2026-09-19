"""Dial our own socket the way the harness does.

This is the readiness check for problem 2: the same Twilio Media Streams
handshake, N sockets at once, each with its own callSid, and a report of how
many held up. Run it before a Run All, not after.

  python scripts/mock_call.py                     one call
  python scripts/mock_call.py --calls 20          the largest burst in the set
  python scripts/mock_call.py --wav turn.wav      send real speech
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import sys
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import websockets  # noqa: E402

from src.voice.audio import (  # noqa: E402
    FRAME_BYTES,
    FRAME_MS,
    frames,
    pcm16_to_ulaw,
    resample_pcm16,
    silence,
)


@dataclass
class Outcome:
    call_id: str
    connected: bool = False
    connect_ms: int = 0
    first_audio_ms: int | None = None
    frames_in: int = 0
    frames_out: int = 0
    cleared: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.connected and self.error is None and self.first_audio_ms is not None


def load_ulaw(path: Path) -> bytes:
    """Read a WAV of any rate and hand back 8 kHz µ-law."""
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
            raise SystemExit("the WAV must be 16-bit mono")
        pcm = handle.readframes(handle.getnframes())
        return pcm16_to_ulaw(resample_pcm16(pcm, handle.getframerate()))


async def one_call(url: str, index: int, seconds: float, payload: bytes | None) -> Outcome:
    call_id = str(uuid.uuid4())
    outcome = Outcome(call_id=call_id)
    started = time.perf_counter()

    try:
        async with websockets.connect(url, open_timeout=10, close_timeout=5) as socket:
            outcome.connected = True
            outcome.connect_ms = int((time.perf_counter() - started) * 1000)
            stream_sid = f"MZ{uuid.uuid4().hex[:30]}"

            async def receive() -> None:
                async for raw in socket:
                    try:
                        message = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if message.get("event") == "media":
                        outcome.frames_in += 1
                        if outcome.first_audio_ms is None:
                            outcome.first_audio_ms = int((time.perf_counter() - started) * 1000)
                    elif message.get("event") == "clear":
                        outcome.cleared += 1

            reader = asyncio.create_task(receive())

            # The wire, in order, exactly as Twilio sends it: strings, camelCase.
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
                        "from_number": f"+346{10000000 + index:08d}"[:12],
                    },
                },
            }))

            audio = payload if payload is not None else silence(int(seconds * 1000))
            sequence = 2
            tick = time.perf_counter()
            for chunk in frames(audio, FRAME_BYTES):
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
                outcome.frames_out += 1
                sequence += 1
                # Real time, because that is the only pace a call has.
                tick += FRAME_MS / 1000
                delay = tick - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)

            await socket.send(json.dumps({
                "event": "stop", "sequenceNumber": str(sequence), "streamSid": stream_sid,
                "stop": {"accountSid": f"AC{uuid.uuid4().hex[:30]}", "callSid": call_id},
            }))
            await asyncio.sleep(1.0)
            reader.cancel()
    except Exception as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"

    return outcome


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:7860/ws")
    parser.add_argument("--calls", type=int, default=1, help="how many sockets at once")
    parser.add_argument("--seconds", type=float, default=8.0, help="how long to hold each one")
    parser.add_argument("--wav", type=Path, default=None, help="16-bit mono WAV to send as the caller")
    args = parser.parse_args()

    payload = load_ulaw(args.wav) if args.wav else None
    if payload is not None:
        args.seconds = len(payload) / 8000

    print(f"Dialling {args.url} · {args.calls} call(s) · {args.seconds:.1f}s each"
          f"{' · real audio' if payload else ' · silence'}\n")

    started = time.perf_counter()
    outcomes = await asyncio.gather(
        *(one_call(args.url, index, args.seconds, payload) for index in range(args.calls))
    )
    elapsed = time.perf_counter() - started

    for outcome in outcomes:
        mark = "ok  " if outcome.ok else "FAIL"
        detail = outcome.error or (
            f"connect {outcome.connect_ms}ms · first audio "
            f"{outcome.first_audio_ms if outcome.first_audio_ms is not None else '—'}ms · "
            f"{outcome.frames_in} frames in / {outcome.frames_out} out"
            + (f" · {outcome.cleared} clear" if outcome.cleared else "")
        )
        print(f"  {mark}  {outcome.call_id[:8]}  {detail}")

    good = [outcome for outcome in outcomes if outcome.ok]
    first_audio = [o.first_audio_ms for o in good if o.first_audio_ms is not None]
    print(f"\n{len(good)}/{len(outcomes)} calls held up in {elapsed:.1f}s")
    if first_audio:
        print(f"time to the agent's first audio: median {int(statistics.median(first_audio))} ms, "
              f"worst {max(first_audio)} ms")
    silent = [o for o in outcomes if o.connected and o.first_audio_ms is None]
    if silent:
        print(f"WARNING: {len(silent)} call(s) produced no audio at all — a silent call is a "
              f"failed case, whatever else it did")
    return 0 if len(good) == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
