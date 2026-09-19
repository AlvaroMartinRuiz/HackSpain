"""Turning the caller's audio into turns.

ElevenLabs Scribe (and Deepgram as a switch) stream and do their own
endpointing, which is what makes barge-in possible. The Whisper path is there
so a team with only one API key can still take a call, at the cost of waiting
for the pause.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
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
ELEVENLABS_STT_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"

log = logging.getLogger("socketwizard")

# Long enough for Deepgram's closing transcript, short enough to stay well
# inside the submission window.
DRAIN_TIMEOUT_S = 2.0
# 100 ms of µ-law: ElevenLabs wants 0.1–1 s chunks, not 20 ms phone frames.
ELEVENLABS_CHUNK_BYTES = 800
ELEVENLABS_ERRORS = {
    "error", "auth_error", "quota_exceeded", "commit_throttled", "unaccepted_terms",
    "rate_limited", "queue_overflow", "resource_exhausted", "session_time_limit_exceeded",
    "input_error", "invalid_request", "chunk_size_exceeded", "insufficient_audio_activity",
    "transcriber_error",
}


class DeepgramTranscriber:
    def __init__(
        self,
        on_partial: OnPartial,
        on_final: OnFinal,
        on_speech_started: Optional[OnSpeechStart] = None,
        on_notice: Optional[OnNotice] = None,
        keyterms: Optional[list[str]] = None,
    ) -> None:
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_speech_started = on_speech_started
        self.on_notice = on_notice
        self._keyterms = list(keyterms or [])
        self._socket = None
        self._reader: Optional[asyncio.Task] = None
        self._keepalive: Optional[asyncio.Task] = None
        self._pending: list[str] = []
        self._language: Optional[str] = None
        self._stream_language = settings.deepgram_language
        self._closed = False
        self.bytes_sent = 0

    def _url(self, language: Optional[str] = None) -> str:
        params: list[tuple[str, str]] = [
            ("model", settings.deepgram_model),
            ("language", language or self._stream_language),
            ("encoding", "mulaw"),
            ("sample_rate", "8000"),
            ("channels", "1"),
            ("interim_results", "true"),
            ("punctuate", "true"),
            ("smart_format", "true"),
            ("vad_events", "true"),
            ("endpointing", str(settings.stt_endpointing_ms)),
            ("utterance_end_ms", str(settings.stt_utterance_end_ms)),
        ]
        if settings.stt_numerals:
            params.append(("numerals", "true"))
        for term in self._keyterms:
            params.append(("keyterm", term))
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
        previous_reader = self._reader
        self._socket = replacement
        self._stream_language = target
        if target == "ca":
            self._language = "ca"
        self._reader = asyncio.create_task(self._read(replacement))
        if previous_reader is not None and not previous_reader.done():
            previous_reader.cancel()
            try:
                await previous_reader
            except (asyncio.CancelledError, Exception):
                pass
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


# Scribe labels languages in ISO 639-3; the rest of the call speaks ISO 639-1.
_ISO3_TO_ISO1 = {"spa": "es", "cat": "ca", "eng": "en", "glg": "gl", "eus": "eu", "baq": "eu"}


class ElevenLabsTranscriber:
    """Scribe v2 realtime: µ-law 8 kHz in, partials out, VAD commits the turn."""

    def __init__(
        self,
        on_partial: OnPartial,
        on_final: OnFinal,
        on_speech_started: Optional[OnSpeechStart] = None,
        on_notice: Optional[OnNotice] = None,
        keyterms: Optional[list[str]] = None,
    ) -> None:
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_speech_started = on_speech_started
        self.on_notice = on_notice
        self._keyterms = list(keyterms or [])
        self._socket = None
        self._reader: Optional[asyncio.Task] = None
        self._language: Optional[str] = None
        self._stream_language: Optional[str] = None
        self._closed = False
        self._first_chunk = True
        self._pending = bytearray()
        self._last_committed = ""
        self._heard_speech = False
        self.bytes_sent = 0

    def _url(self, language: Optional[str] = None) -> str:
        silence_s = max(0.3, min(1.5, settings.stt_utterance_end_ms / 1000.0))
        params: list[tuple[str, str]] = [
            ("model_id", settings.elevenlabs_stt_model),
            ("audio_format", "ulaw_8000"),
            ("commit_strategy", "vad"),
            ("vad_silence_threshold_secs", f"{silence_s:.2f}"),
            ("min_speech_duration_ms", "100"),
            ("min_silence_duration_ms", str(settings.stt_endpointing_ms)),
        ]
        if settings.stt_filter_background:
            params.append(("filter_background_audio", "true"))
        locked = (language or "").lower()[:2]
        if locked == "ca":
            params.append(("language_code", "ca"))
        else:
            params.append(("include_language_detection", "true"))
            for code in ("es", "en", "ca"):
                params.append(("secondary_languages", code))
        for term in self._keyterms[:50]:
            clipped = term.strip()[:20]
            if clipped:
                params.append(("keyterms", clipped))
        return f"{ELEVENLABS_STT_URL}?{urllib.parse.urlencode(params)}"

    async def _connect(self, language: Optional[str] = None):
        import websockets

        headers = {"xi-api-key": settings.elevenlabs_api_key}
        try:
            return await websockets.connect(self._url(language), additional_headers=headers)
        except TypeError:
            return await websockets.connect(self._url(language), extra_headers=headers)

    async def start(self) -> None:
        self._socket = await self._connect(self._stream_language)
        self._reader = asyncio.create_task(self._read(self._socket))
        self._first_chunk = True

    async def set_stream_language(self, language: str) -> None:
        """Lock Catalan; anything else stays auto-detected on this call."""
        target = "ca" if language.lower().startswith("ca") else None
        if self._closed or target == self._stream_language:
            return

        await self._send_chunk(commit=False)
        replacement = await self._connect(target)
        previous = self._socket
        previous_reader = self._reader
        self._socket = replacement
        self._stream_language = target
        if target == "ca":
            self._language = "ca"
        self._first_chunk = True
        self._heard_speech = False
        self._reader = asyncio.create_task(self._read(replacement))
        if previous_reader is not None and not previous_reader.done():
            previous_reader.cancel()
            try:
                await previous_reader
            except (asyncio.CancelledError, Exception):
                pass
        if previous is not None:
            try:
                await previous.close()
            except Exception:
                pass
        if self.on_notice is not None:
            await self.on_notice("stt_language_switch", {"language": target or "auto"})

    async def push(self, ulaw_frame: bytes) -> None:
        if self._socket is None or self._closed:
            return
        self._pending.extend(ulaw_frame)
        self.bytes_sent += len(ulaw_frame)
        if len(self._pending) >= ELEVENLABS_CHUNK_BYTES:
            await self._send_chunk(commit=False)

    async def _send_chunk(self, commit: bool) -> None:
        if self._socket is None or self._closed:
            self._pending.clear()
            return
        audio = bytes(self._pending)
        self._pending.clear()
        if not audio and not commit:
            return
        if not audio:
            audio = b"\xff" * 160
        message: dict[str, object] = {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(audio).decode("ascii"),
            "commit": commit,
            "sample_rate": 8000,
        }
        if self._first_chunk:
            context = (self._last_committed or settings.greeting)[:50]
            if context:
                message["previous_text"] = context
            self._first_chunk = False
        try:
            await self._socket.send(json.dumps(message))
        except Exception as exc:
            self._closed = True
            log.warning("elevenlabs stt push failed after %s bytes: %s", self.bytes_sent, exc)

    async def finish(self) -> None:
        try:
            await self._send_chunk(commit=True)
        except Exception:
            pass
        self._closed = True
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._reader, timeout=DRAIN_TIMEOUT_S)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._reader.cancel()
                try:
                    await self._reader
                except (asyncio.CancelledError, Exception):
                    pass
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
            log.exception("elevenlabs stt reader stopped: %s: %s", type(exc).__name__, exc)
            if self.on_notice is not None:
                await self.on_notice("elevenlabs_reader_stopped", {
                    "error": f"{type(exc).__name__}: {exc}",
                    "bytes_sent": self.bytes_sent,
                })

    async def _handle(self, message: dict) -> None:
        kind = str(message.get("message_type") or "")

        if kind in ELEVENLABS_ERRORS or message.get("error"):
            log.error("elevenlabs stt %s: %s", kind, message.get("error") or message)
            if self.on_notice is not None:
                await self.on_notice("elevenlabs_error", {
                    "kind": kind or "error",
                    "message": str(message.get("error") or message)[:400],
                })
            return

        if kind == "session_started":
            if self.on_notice is not None:
                await self.on_notice("elevenlabs_session", {
                    "session_id": message.get("session_id"),
                    "config": message.get("config") or {},
                })
            return

        if kind == "warning":
            if self.on_notice is not None:
                await self.on_notice("elevenlabs_warning", {
                    "message": str(message.get("warning") or "")[:400],
                })
            return

        text = (message.get("text") or "").strip()
        language = message.get("language_code")
        if language:
            # Scribe answers in ISO 639-3: "spa"[:2] is "sp", not Spanish.
            code = str(language).lower()
            self._language = _ISO3_TO_ISO1.get(code, code.split("-")[0][:2])

        if kind == "partial_transcript":
            if text:
                if not self._heard_speech and self.on_speech_started is not None:
                    self._heard_speech = True
                    await self.on_speech_started()
                await self.on_partial(text)
            return

        if kind in {"committed_transcript", "committed_transcript_with_timestamps"}:
            self._heard_speech = False
            if text:
                self._last_committed = text
                await self.on_final(text, self._language)
            return


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
    keyterms: Optional[list[str]] = None,
):
    provider = settings.stt_provider
    if provider == "elevenlabs" and settings.elevenlabs_api_key:
        return ElevenLabsTranscriber(
            on_partial, on_final, on_speech_started, on_notice, keyterms=keyterms
        )
    if provider == "deepgram" and settings.deepgram_api_key:
        return DeepgramTranscriber(
            on_partial, on_final, on_speech_started, on_notice, keyterms=keyterms
        )
    return WhisperTranscriber(on_partial, on_final, on_speech_started)


def keyterms_for(catalog) -> list[str]:
    """Clinic vocabulary the transcriber should prefer over a near-homophone.

    Deepgram Nova-3 and ElevenLabs Scribe both take repeated keyterm parameters
    at connect time. Surnames a phone line cannot tell apart, site names and
    the streets in the public cases are the ones that cost points.
    """
    terms: list[str] = [
        "Clínica Arenal", "Arenal Centro", "Arenal Norte", "Arenal Sur",
        "Getafe", "Sanitas", "Adeslas", "DKV", "ASISA", "Mapfre Salud",
        "Caser", "Cigna", "AXA", "Nueva Mutua",
        "Puerta del Sol", "Preciados", "Plaza de Castilla", "Castellana",
        "Gran Vía", "Alberto Alcocer",
        "médico de cabecera", "traumatólogo", "ginecóloga", "pediatra",
        "metge de capçalera", "Sáez", "Sáenz", "Iglesias", "Iglesia",
        "Requena", "Montoro",
    ]
    if catalog is not None:
        for provider in catalog.providers.values():
            name = re.sub(r"^(?:dra?|doctora|doctor)\.?\s+", "", provider.name, flags=re.I).strip()
            if name:
                terms.append(name)
        for location in catalog.locations.values():
            terms.append(location.name)
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        key = term.casefold()
        if key in seen or not term.strip():
            continue
        seen.add(key)
        unique.append(term.strip())
    return unique[:80]


__all__ = [
    "build_transcriber",
    "DeepgramTranscriber",
    "ElevenLabsTranscriber",
    "WhisperTranscriber",
    "FRAME_MS",
    "keyterms_for",
]
