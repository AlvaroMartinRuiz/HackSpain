"""Turning the agent's words into µ-law the line can carry.

Each provider streams, so the first frames go out while the rest is still
being synthesised. ElevenLabs and Cartesia return µ-law directly; OpenAI
returns PCM and is converted here.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

import httpx

from src.config import settings
from src.voice.audio import pcm16_to_ulaw, resample_pcm16, silence

_TTS_CONCURRENCY = asyncio.Semaphore(10)
_SUPPORTED_ELEVENLABS_LANGUAGES = {"en", "es"}


class Synthesizer:
    async def stream(
        self, text: str, language: str = "es"
    ) -> AsyncIterator[bytes]:  # pragma: no cover - interface
        raise NotImplementedError
        yield b""

    async def aclose(self) -> None:
        return None


class ElevenLabsSynthesizer(Synthesizer):
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://api.elevenlabs.io",
            headers={"xi-api-key": settings.elevenlabs_api_key},
            timeout=httpx.Timeout(30.0, connect=6.0),
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=30),
        )

    async def stream(self, text: str, language: str = "es") -> AsyncIterator[bytes]:
        path = f"/v1/text-to-speech/{settings.elevenlabs_voice_id}/stream"
        params = {"output_format": "ulaw_8000", "optimize_streaming_latency": "3"}
        body = {
            "text": text,
            "model_id": settings.elevenlabs_model,
            "voice_settings": {"stability": 0.4, "similarity_boost": 0.7, "speed": 1.0},
        }
        code = language.lower()[:2]
        if code in _SUPPORTED_ELEVENLABS_LANGUAGES:
            body["language_code"] = code

        for attempt in range(2):
            retry = False
            async with _TTS_CONCURRENCY:
                async with self._client.stream("POST", path, params=params, json=body) as response:
                    if response.status_code == 429 and attempt == 0:
                        await response.aread()
                        retry = True
                    elif response.status_code != 200:
                        await response.aread()
                        raise RuntimeError(f"elevenlabs HTTP {response.status_code}")
                    else:
                        async for chunk in response.aiter_bytes(chunk_size=1600):
                            if chunk:
                                yield chunk
                        return
            if retry:
                await asyncio.sleep(0.35)
        raise RuntimeError("elevenlabs HTTP 429")

    async def aclose(self) -> None:
        await self._client.aclose()


class DeepgramAuraSynthesizer(Synthesizer):
    """Aura-2 returns raw telephony µ-law, so no local conversion is needed."""

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

    async def stream(self, text: str, language: str = "es") -> AsyncIterator[bytes]:
        model = (
            settings.deepgram_tts_model_en
            if language.lower().startswith("en")
            else settings.deepgram_tts_model_es
        )
        params = {
            "model": model,
            "encoding": "mulaw",
            "sample_rate": "8000",
            "container": "none",
        }
        for attempt in range(2):
            retry = False
            async with _TTS_CONCURRENCY:
                async with self._client.stream(
                    "POST", "/v1/speak", params=params, json={"text": text}
                ) as response:
                    if response.status_code == 429 and attempt == 0:
                        await response.aread()
                        retry = True
                    elif response.status_code != 200:
                        await response.aread()
                        raise RuntimeError(f"deepgram tts HTTP {response.status_code}")
                    else:
                        async for chunk in response.aiter_bytes(chunk_size=1600):
                            if chunk:
                                yield chunk
                        return
            if retry:
                await asyncio.sleep(0.35)
        raise RuntimeError("deepgram tts HTTP 429")

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

    async def stream(self, text: str, language: str = "es") -> AsyncIterator[bytes]:
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

    async def stream(self, text: str, language: str = "es") -> AsyncIterator[bytes]:
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

    async def stream(self, text: str, language: str = "es") -> AsyncIterator[bytes]:
        # Roughly the length the words would have taken, so timing stays honest.
        duration_ms = min(6000, max(400, len(text) * 55))
        yield silence(duration_ms)


class FallbackSynthesizer(Synthesizer):
    """Try the backup only when the primary failed before producing audio."""

    def __init__(self, providers: list[Synthesizer]) -> None:
        self.providers = providers

    async def stream(self, text: str, language: str = "es") -> AsyncIterator[bytes]:
        last_error: Optional[Exception] = None
        for provider in self.providers:
            emitted = False
            try:
                async for chunk in provider.stream(text, language):
                    emitted = True
                    yield chunk
                return
            except Exception as exc:
                last_error = exc
                if emitted:
                    raise
        raise RuntimeError("all configured TTS providers failed") from last_error

    async def aclose(self) -> None:
        for provider in self.providers:
            await provider.aclose()


def build_synthesizer() -> Synthesizer:
    provider = settings.tts_provider
    candidates: list[Synthesizer] = []

    def add(name: str) -> None:
        if name == "deepgram" and settings.deepgram_api_key:
            candidates.append(DeepgramAuraSynthesizer())
        elif name == "elevenlabs" and settings.elevenlabs_api_key:
            candidates.append(ElevenLabsSynthesizer())
        elif name == "cartesia" and settings.cartesia_api_key and settings.cartesia_voice_id:
            candidates.append(CartesiaSynthesizer())
        elif name == "openai" and settings.llm_api_key:
            candidates.append(OpenAISynthesizer())

    add(provider)
    for fallback in ("deepgram", "elevenlabs"):
        if fallback != provider:
            add(fallback)
    if not candidates:
        add("openai")
    if not candidates:
        return SilentSynthesizer()
    if len(candidates) == 1:
        return candidates[0]
    return FallbackSynthesizer(candidates)


def active_provider_name() -> str:
    synthesizer: Optional[Synthesizer] = None
    try:
        synthesizer = build_synthesizer()
        active = synthesizer.providers[0] if isinstance(synthesizer, FallbackSynthesizer) else synthesizer
        return type(active).__name__.replace("Synthesizer", "").lower()
    finally:
        del synthesizer
