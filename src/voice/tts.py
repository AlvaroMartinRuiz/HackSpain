"""Turning the agent's words into µ-law the line can carry.

Each provider streams, so the first frames go out while the rest is still
being synthesised. Deepgram Aura, ElevenLabs and Cartesia return µ-law
directly; OpenAI returns PCM and is converted here. ElevenLabs is the default;
Aura is the automatic fallback if Flash 429s.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import AsyncIterator, Optional

import httpx

from src.agent import phrases
from src.config import settings
from src.voice.audio import pcm16_to_ulaw, resample_pcm16, silence

log = logging.getLogger("socketwizard")

# One gate per account, because each has its own limit: when ElevenLabs is full
# the fallback to Aura must not queue behind it. Creator allows 10 concurrent
# Flash requests.
_GATES = {
    "ElevenLabsSynthesizer": asyncio.Semaphore(int(os.getenv("ELEVENLABS_CONCURRENCY", "10"))),
    "DeepgramSynthesizer": asyncio.Semaphore(int(os.getenv("DEEPGRAM_TTS_CONCURRENCY", "15"))),
}
_OPEN_GATE = asyncio.Semaphore(50)
_SUPPORTED_ELEVENLABS_LANGUAGES = {"ca", "en", "es"}


def deepgram_model_for_language(language: str) -> str:
    return (
        settings.deepgram_tts_model_en
        if language.lower().startswith("en")
        else settings.deepgram_tts_model_es
    )


def elevenlabs_model_for_language(language: str) -> str:
    return (
        settings.elevenlabs_catalan_model
        if language.lower().startswith("ca")
        else settings.elevenlabs_model
    )

# Audio for the lines that never change (greeting, silence prompts, retry and
# hold), synthesised once. Written at start-up or on first use and never
# modified, so sharing it between calls shares no call state. The key carries
# the voice settings, so a changed voice never plays a stale recording.
_AUDIO_CACHE: dict[tuple, bytes] = {}


def _gate(synth: "Synthesizer") -> asyncio.Semaphore:
    return _GATES.get(type(synth).__name__, _OPEN_GATE)


def _cache_key(text: str, language: str) -> tuple:
    return (settings.tts_provider, settings.elevenlabs_voice_id, settings.elevenlabs_model,
            settings.elevenlabs_voice_id_en, settings.elevenlabs_voice_id_es,
            settings.elevenlabs_catalan_model,
            settings.deepgram_tts_model_es, settings.deepgram_tts_model_en,
            (language or "")[:2], text)


def is_fixed_line(text: str) -> bool:
    if text == settings.greeting:
        return True
    return any(text in phrases.fixed_lines(code) for code in phrases.SILENCE_PROMPTS)


def cached_audio(text: str, language: str) -> Optional[bytes]:
    return _AUDIO_CACHE.get(_cache_key(text, language))


def remember_audio(text: str, language: str, audio: bytes) -> None:
    if audio and is_fixed_line(text):
        _AUDIO_CACHE[_cache_key(text, language)] = audio


async def warm_cache() -> int:
    """Synthesise the fixed lines before the first call needs them.

    Ten calls of a Run All greet in the same second; with the greeting cached
    that burst never reaches the TTS provider. Catalan is left to first use.
    """
    synth = build_synthesizer()
    warmed = 0
    try:
        jobs = [(settings.greeting, settings.default_language)]
        for code in ("en", "es"):
            jobs += [(line, code) for line in phrases.fixed_lines(code) if line]
        for text, language in jobs:
            if cached_audio(text, language):
                continue
            audio = b"".join([chunk async for chunk in synth.stream(text, language)])
            remember_audio(text, language, audio)
            warmed += 1
    finally:
        await synth.aclose()
    return warmed


def elevenlabs_voice_for_language(language: str) -> str:
    code = language.lower()[:2]
    if code == "en":
        return settings.elevenlabs_voice_id_en or settings.elevenlabs_voice_id
    return settings.elevenlabs_voice_id_es or settings.elevenlabs_voice_id


_SPANISH_SCRIPT = re.compile(r"[áéíóúñ¿¡]", re.IGNORECASE)


def speech_language(text: str, fallback: str) -> str:
    """Voice follows the line being spoken, not a stale session lock."""
    code = (fallback or "es")[:2]
    if _SPANISH_SCRIPT.search(text or ""):
        return "es" if code != "ca" else "ca"
    return code if code in {"es", "en", "ca"} else "es"


class Synthesizer:
    async def stream(
        self, text: str, language: str = "es", speed: float = 1.0
    ) -> AsyncIterator[bytes]:  # pragma: no cover - interface
        raise NotImplementedError
        yield b""

    async def aclose(self) -> None:
        return None


class DeepgramSynthesizer(Synthesizer):
    """Aura, which speaks µ-law at 8 kHz natively and costs a fraction."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://api.deepgram.com",
            headers={
                "Authorization": f"Token {settings.deepgram_api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=6.0),
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=30),
        )

    async def stream(self, text: str, language: str = "es", speed: float = 1.0) -> AsyncIterator[bytes]:
        model = deepgram_model_for_language(language)
        params = {
            "model": model,
            "encoding": "mulaw",
            "sample_rate": "8000",
            # Without this the WAV header arrives as audio and the line clicks.
            "container": "none",
        }
        async with self._client.stream(
            "POST", "/v1/speak", params=params, json={"text": text}
        ) as response:
            if response.status_code != 200:
                detail = (await response.aread()).decode("utf-8", "replace")[:240]
                raise RuntimeError(f"deepgram tts HTTP {response.status_code}: {detail}")
            async for chunk in response.aiter_bytes(chunk_size=1600):
                if chunk:
                    yield chunk

    async def aclose(self) -> None:
        await self._client.aclose()


class ElevenLabsSynthesizer(Synthesizer):
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://api.elevenlabs.io",
            headers={"xi-api-key": settings.elevenlabs_api_key},
            timeout=httpx.Timeout(30.0, connect=6.0),
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=30),
        )

    async def stream(self, text: str, language: str = "es", speed: float = 1.0) -> AsyncIterator[bytes]:
        path = f"/v1/text-to-speech/{elevenlabs_voice_for_language(language)}/stream"
        code = language.lower()[:2]
        model = elevenlabs_model_for_language(code)
        params = {"output_format": "ulaw_8000"}
        if not model.startswith("eleven_v3"):
            params["optimize_streaming_latency"] = "3"
        body = {
            "text": text,
            "model_id": model,
            "voice_settings": {
                "stability": 0.4,
                "similarity_boost": 0.7,
                "speed": max(0.7, min(1.2, speed or 1.0)),
            },
        }
        if code in _SUPPORTED_ELEVENLABS_LANGUAGES:
            body["language_code"] = code

        async with self._client.stream("POST", path, params=params, json=body) as response:
            if response.status_code != 200:
                detail = (await response.aread()).decode("utf-8", "replace")[:240]
                raise RuntimeError(f"elevenlabs HTTP {response.status_code}: {detail}")
            async for chunk in response.aiter_bytes(chunk_size=1600):
                if chunk:
                    yield chunk

    async def aclose(self) -> None:
        await self._client.aclose()


class CartesiaSynthesizer(Synthesizer):
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://api.cartesia.ai",
            headers={
                "X-API-Key": settings.cartesia_api_key,
                "Cartesia-Version": "2024-06-10",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0, connect=6.0),
        )

    async def stream(self, text: str, language: str = "es", speed: float = 1.0) -> AsyncIterator[bytes]:
        body = {
            "model_id": settings.cartesia_model,
            "transcript": text,
            "voice": {"mode": "id", "id": settings.cartesia_voice_id},
            "output_format": {"container": "raw", "encoding": "pcm_mulaw", "sample_rate": 8000},
            "language": "es",
        }
        async with self._client.stream("POST", "/tts/bytes", json=body) as response:
            if response.status_code != 200:
                await response.aread()
                raise RuntimeError(f"cartesia HTTP {response.status_code}")
            async for chunk in response.aiter_bytes(chunk_size=1600):
                if chunk:
                    yield chunk

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenAISynthesizer(Synthesizer):
    """PCM at 24 kHz on the way in, µ-law at 8 kHz on the way out."""

    SOURCE_RATE = 24000

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            timeout=httpx.Timeout(30.0, connect=6.0),
        )

    async def stream(self, text: str, language: str = "es", speed: float = 1.0) -> AsyncIterator[bytes]:
        body = {
            "model": settings.openai_tts_model,
            "voice": settings.openai_tts_voice,
            "input": text,
            "response_format": "pcm",
        }
        tail = b""
        async with self._client.stream("POST", "/audio/speech", json=body) as response:
            if response.status_code != 200:
                await response.aread()
                raise RuntimeError(f"openai tts HTTP {response.status_code}")
            async for chunk in response.aiter_bytes(chunk_size=9600):
                if not chunk:
                    continue
                buffer = tail + chunk
                # Keep whole samples together across chunk boundaries.
                usable = len(buffer) - (len(buffer) % 2)
                tail = buffer[usable:]
                pcm = resample_pcm16(buffer[:usable], self.SOURCE_RATE)
                if pcm:
                    yield pcm16_to_ulaw(pcm)

    async def aclose(self) -> None:
        await self._client.aclose()


class SilentSynthesizer(Synthesizer):
    """No keys configured: keeps the pipeline testable without making sound."""

    async def stream(self, text: str, language: str = "es", speed: float = 1.0) -> AsyncIterator[bytes]:
        # Roughly the length the words would have taken, so timing stays honest.
        duration_ms = min(6000, max(400, len(text) * 55))
        yield silence(duration_ms)


class ResilientSynthesizer(Synthesizer):
    """Semaphore, one 429 retry, then the next provider. Never burns a
    reserved voice on a practice provider's failure."""

    def __init__(self, primary: Synthesizer, fallbacks: Optional[list[Synthesizer]] = None) -> None:
        self._chain = [primary] + list(fallbacks or [])

    async def stream(self, text: str, language: str = "es", speed: float = 1.0) -> AsyncIterator[bytes]:
        last_error: Optional[Exception] = None
        for index, synth in enumerate(self._chain):
            attempts = 2 if index == 0 else 1
            for attempt in range(attempts):
                got_audio = False
                try:
                    async with _gate(synth):
                        async for chunk in synth.stream(text, language):
                            got_audio = True
                            yield chunk
                    return
                except Exception as exc:
                    last_error = exc
                    if got_audio:
                        raise
                    log.warning("tts %s failed (attempt %s): %s", type(synth).__name__, attempt + 1, exc)
                    if attempt + 1 < attempts and _is_retryable(exc):
                        await asyncio.sleep(0.4)
                        continue
                    break
        if last_error is not None:
            raise last_error

    async def aclose(self) -> None:
        for synth in self._chain:
            try:
                await synth.aclose()
            except Exception:
                pass


def _is_retryable(exc: Exception) -> bool:
    text = str(exc)
    return "429" in text or "503" in text or "timeout" in text.lower()


def build_synthesizer() -> Synthesizer:
    provider = settings.tts_provider
    primary: Optional[Synthesizer] = None
    fallbacks: list[Synthesizer] = []

    if provider == "deepgram" and settings.deepgram_api_key:
        primary = DeepgramSynthesizer()
    elif provider == "elevenlabs" and settings.elevenlabs_api_key:
        primary = ElevenLabsSynthesizer()
        # Aura covers a practice/scored failover without burning more ElevenLabs.
        if settings.deepgram_api_key:
            fallbacks.append(DeepgramSynthesizer())
    elif provider == "cartesia" and settings.cartesia_api_key and settings.cartesia_voice_id:
        primary = CartesiaSynthesizer()
    elif provider == "openai" and settings.llm_api_key:
        primary = OpenAISynthesizer()

    if primary is None:
        if settings.elevenlabs_api_key:
            primary = ElevenLabsSynthesizer()
        elif settings.deepgram_api_key:
            primary = DeepgramSynthesizer()
        elif settings.llm_api_key:
            primary = OpenAISynthesizer()
        else:
            return SilentSynthesizer()

    return ResilientSynthesizer(primary, fallbacks)


def active_provider_name() -> str:
    synthesizer: Optional[Synthesizer] = None
    try:
        synthesizer = build_synthesizer()
        inner = synthesizer
        if isinstance(synthesizer, ResilientSynthesizer) and synthesizer._chain:
            inner = synthesizer._chain[0]
        return type(inner).__name__.replace("Synthesizer", "").lower()
    finally:
        del synthesizer
