"""Turning the caller's audio into turns.

Deepgram streams and does its own endpointing, which is what makes barge-in
possible. The Whisper path is there so a team with only one API key can still
take a call, at the cost of waiting for the pause.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from typing import Awaitable, Callable, Optional

import httpx

from src.config import settings
from src.voice.audio import FRAME_MS, ulaw_to_wav
from src.voice.vad import EnergyVAD

OnPartial = Callable[[str], Awaitable[None]]
OnFinal = Callable[[str, Optional[str]], Awaitable[None]]
OnSpeechStart = Callable[[], Awaitable[None]]
OnNotice = Callable[[str, dict], Awaitable[None]]

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"

log = logging.getLogger("elturno")

# Long enough for Deepgram's closing transcript, short enough to stay well
# inside the submission window.
DRAIN_TIMEOUT_S = 2.0


class DeepgramTranscriber:
    def __init__(
        self,
        on_partial: OnPartial,
        on_final: OnFinal,
        on_speech_started: Optional[OnSpeechStart] = None,
        on_notice: Optional[OnNotice] = None,
    ) -> None:
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_speech_started = on_speech_started
        self.on_notice = on_notice
        self._socket = None
        self._reader: Optional[asyncio.Task] = None
        self._keepalive: Optional[asyncio.Task] = None
        self._pending: list[str] = []
        self._language: Optional[str] = None
        self._stream_language = settings.deepgram_language
        self._closed = False
        self.bytes_sent = 0

    def _url(self, language: Optional[str] = None) -> str:
        params = {
            "model": settings.deepgram_model,
            "language": language or self._stream_language,
            "encoding": "mulaw",
            "sample_rate": "8000",
            "channels": "1",
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
            "vad_events": "true",
            "endpointing": str(settings.stt_endpointing_ms),
            "utterance_end_ms": str(settings.stt_utterance_end_ms),
        }
        return f"{DEEPGRAM_URL}?{urllib.parse.urlencode(params)}"

    async def _connect(self, language: Optional[str] = None):
        import websockets

        headers = {"Authorization": f"Token {settings.deepgram_api_key}"}
        try:
            return await websockets.connect(self._url(language), additional_headers=headers)
        except TypeError:  # websockets < 14 named it differently
            return await websockets.connect(self._url(language), extra_headers=headers)

    async def start(self) -> None:
        self._socket = await self._connect()
        self._reader = asyncio.create_task(self._read(self._socket))
        self._keepalive = asyncio.create_task(self._ping())

    async def set_stream_language(self, language: str) -> None:
        """Move this call to a monolingual stream without touching other calls."""
        target = language if language == "ca" else settings.deepgram_language
        if self._closed or target == self._stream_language:
            return

        replacement = await self._connect(target)
        previous = self._socket
        self._socket = replacement
        self._stream_language = target
        if target == "ca":
            self._language = "ca"
        self._reader = asyncio.create_task(self._read(replacement))
        if previous is not None:
            try:
                await previous.close()
            except Exception:
                pass
        if self.on_notice is not None:
            await self.on_notice("stt_language_switch", {"language": target})

    async def push(self, ulaw_frame: bytes) -> None:
        if self._socket is None or self._closed:
            return
        try:
            await self._socket.send(ulaw_frame)
            self.bytes_sent += len(ulaw_frame)
        except Exception as exc:
            self._closed = True
            log.warning("deepgram push failed after %s bytes: %s", self.bytes_sent, exc)

    async def finish(self) -> None:
        self._closed = True
        if self._keepalive is not None:
            self._keepalive.cancel()
        if self._socket is not None:
            try:
                await self._socket.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass
        if self._reader is not None:
            # Deepgram sends the closing transcript after CloseStream, so the
            # caller's last words are lost if we cancel straight away.
            try:
                await asyncio.wait_for(self._reader, timeout=DRAIN_TIMEOUT_S)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._reader.cancel()
                try:
                    await self._reader
                except (asyncio.CancelledError, Exception):
                    pass
        await self._flush()
        if self._socket is not None:
            try:
                await self._socket.close()
            except Exception:
                pass

    async def _read(self, socket) -> None:
        try:
            async for raw in socket:
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                await self._handle(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if socket is not self._socket:
                return
            # Dying quietly here would leave the agent deaf for the rest of the
            # call with nothing to show why.
            log.exception("deepgram reader stopped: %s: %s", type(exc).__name__, exc)
            if self.on_notice is not None:
                await self.on_notice("deepgram_reader_stopped", {
                    "error": f"{type(exc).__name__}: {exc}",
                    "bytes_sent": self.bytes_sent,
                })

    async def _ping(self) -> None:
        """Deepgram drops a listen socket after ~10 s of silence."""
        try:
            while not self._closed and self._socket is not None:
                await asyncio.sleep(5)
                if self._closed or self._socket is None:
                    return
                try:
                    await self._socket.send(json.dumps({"type": "KeepAlive"}))
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    async def _handle(self, message: dict) -> None:
        kind = message.get("type")

        if kind in {"Error", "Warning"}:
            log.error("deepgram %s: %s", kind, message)
            if self.on_notice is not None:
                await self.on_notice("deepgram_error", {
                    "kind": kind,
                    "message": str(message.get("message") or message.get("description") or message)[:400],
                })
            return

        if kind == "Metadata":
            if self.on_notice is not None:
                await self.on_notice("deepgram_meta", {
                    "duration": message.get("duration"),
                    "channels": message.get("channels"),
                    "request_id": message.get("request_id"),
                })
            return

        if kind == "SpeechStarted":
            if self.on_speech_started is not None:
                await self.on_speech_started()
            return

        if kind == "UtteranceEnd":
            await self._flush()
            return

        if kind != "Results":
            return

        channel = message.get("channel") or {}
        alternatives = channel.get("alternatives") or [{}]
        transcript = (alternatives[0].get("transcript") or "").strip()
        languages = alternatives[0].get("languages") or channel.get("languages")
        if languages:
            self._language = str(languages[0])[:2]

        if not transcript:
            return

        if message.get("is_final"):
            self._pending.append(transcript)
            if message.get("speech_final"):
                await self._flush()
        else:
            # An interim result is the earliest sign the caller has started.
            await self.on_partial(transcript)

    async def _flush(self) -> None:
        text = " ".join(part for part in self._pending if part).strip()
        self._pending.clear()
        if text:
            await self.on_final(text, self._language)


class WhisperTranscriber:
    """Buffers a turn behind an energy gate, then transcribes it in one go."""

    def __init__(
        self,
        on_partial: OnPartial,
        on_final: OnFinal,
        on_speech_started: Optional[OnSpeechStart] = None,
    ) -> None:
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_speech_started = on_speech_started
        self._vad = EnergyVAD()
        self._buffer = bytearray()
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            timeout=httpx.Timeout(30.0, connect=6.0),
        )
        self._announced = False

    async def start(self) -> None:
        return None

    async def push(self, ulaw_frame: bytes) -> None:
        event = self._vad.feed(ulaw_frame)
        if self._vad.speaking:
            self._buffer.extend(ulaw_frame)
        if event == "start":
            self._announced = False
            if self.on_speech_started is not None:
                await self.on_speech_started()
        elif event == "start" or (self._vad.speaking and not self._announced):
            self._announced = True
        elif event == "end":
            await self._transcribe()

    async def finish(self) -> None:
        if self._buffer:
            await self._transcribe()
        await self._client.aclose()

    async def _transcribe(self) -> None:
        audio = bytes(self._buffer)
        self._buffer.clear()
        # Anything under a third of a second is a cough, not a turn.
        if len(audio) < (8000 // 1000) * 300:
            return
        try:
            response = await self._client.post(
                "/audio/transcriptions",
                files={"file": ("turn.wav", ulaw_to_wav(audio), "audio/wav")},
                data={"model": "whisper-1"},
            )
            if response.status_code != 200:
                return
            text = (response.json().get("text") or "").strip()
        except Exception:
            return
        if text:
            await self.on_final(text, None)


def build_transcriber(
    on_partial: OnPartial,
    on_final: OnFinal,
    on_speech_started: Optional[OnSpeechStart] = None,
    on_notice: Optional[OnNotice] = None,
):
    provider = settings.stt_provider
    if provider == "deepgram" and settings.deepgram_api_key:
        return DeepgramTranscriber(on_partial, on_final, on_speech_started, on_notice)
    return WhisperTranscriber(on_partial, on_final, on_speech_started)


__all__ = ["build_transcriber", "DeepgramTranscriber", "WhisperTranscriber", "FRAME_MS"]
