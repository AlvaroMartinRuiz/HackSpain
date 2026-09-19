"""Turning the agent's words into µ-law the line can carry.

Each provider streams, so the first frames go out while the rest is still
being synthesised. ElevenLabs and Cartesia return µ-law directly; OpenAI
returns PCM and is converted here.
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

import httpx

from src.config import settings
from src.voice.audio import pcm16_to_ulaw, resample_pcm16, silence


class Synthesizer:
    async def stream(self, text: str) -> AsyncIterator[bytes]:  # pragma: no cover - interface
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

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        path = f"/v1/text-to-speech/{settings.elevenlabs_voice_id}/stream"
        params = {"output_format": "ulaw_8000", "optimize_streaming_latency": "3"}
        body = {
            "text": text,
            "model_id": settings.elevenlabs_model,
            "voice_settings": {"stability": 0.4, "similarity_boost": 0.7, "speed": 1.0},
        }
        async with self._client.stream("POST", path, params=params, json=body) as response:
            if response.status_code != 200:
                await response.aread()
                raise RuntimeError(f"elevenlabs HTTP {response.status_code}")
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

    async def stream(self, text: str) -> AsyncIterator[bytes]:
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

    async def stream(self, text: str) -> AsyncIterator[bytes]:
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

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        # Roughly the length the words would have taken, so timing stays honest.
        duration_ms = min(6000, max(400, len(text) * 55))
        yield silence(duration_ms)


def build_synthesizer() -> Synthesizer:
    provider = settings.tts_provider
    if provider == "elevenlabs" and settings.elevenlabs_api_key:
        return ElevenLabsSynthesizer()
    if provider == "cartesia" and settings.cartesia_api_key and settings.cartesia_voice_id:
        return CartesiaSynthesizer()
    if provider == "openai" and settings.llm_api_key:
        return OpenAISynthesizer()
    if settings.elevenlabs_api_key:
        return ElevenLabsSynthesizer()
    if settings.llm_api_key:
        return OpenAISynthesizer()
    return SilentSynthesizer()


def active_provider_name() -> str:
    synthesizer: Optional[Synthesizer] = None
    try:
        synthesizer = build_synthesizer()
        return type(synthesizer).__name__.replace("Synthesizer", "").lower()
    finally:
        del synthesizer
