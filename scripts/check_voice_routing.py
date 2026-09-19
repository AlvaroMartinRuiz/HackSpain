"""Offline checks for per-call Spanish, Catalan and English voice routing."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings  # noqa: E402
from src.agent.brain import Agent  # noqa: E402
from src.voice.language import decide_language  # noqa: E402
from src.voice.stt import DeepgramTranscriber  # noqa: E402
from src.voice.tts import (  # noqa: E402
    deepgram_model_for_language,
    elevenlabs_model_for_language,
)


async def _noop(*_args) -> None:
    return None


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    return int(ok)


def main() -> int:
    passed = 0
    total = 0

    samples = [
        ("Spanish text", "Hola, necesito una cita por la mañana, por favor.", "es", "es"),
        ("Catalan text", "Bon dia, voldria demanar hora amb el metge, si us plau.", "es", "ca"),
        ("English text", "Hello, I need the earliest appointment please.", "en", "en"),
    ]
    for label, text, hint, expected in samples:
        total += 1
        decision = decide_language(text, hint)
        passed += check(label, decision.code == expected,
                        f"{decision.code} {decision.confidence:.2f} via {decision.source}")

    total += 1
    passed += check(
        "Catalan STT locks to ca",
        parse_qs(urlparse(DeepgramTranscriber(_noop, _noop)._url("ca")).query)["language"] == ["ca"],
    )

    total += 1
    passed += check(
        "English Aura model",
        deepgram_model_for_language("en") == settings.deepgram_tts_model_en,
    )

    total += 1
    passed += check(
        "Catalan ElevenLabs model",
        elevenlabs_model_for_language("ca") == settings.elevenlabs_catalan_model,
        settings.elevenlabs_catalan_model,
    )

    total += 1
    agent = Agent.__new__(Agent)
    agent._buffer = "La cita es con Dra. Elena Iglesias. ¿Le viene bien?"
    chunks = agent._drain()
    passed += check(
        "Doctor titles stay with the name",
        chunks == ["La cita es con Dra. Elena Iglesias.", "¿Le viene bien?"],
        repr(chunks),
    )

    print(f"\n{passed}/{total} voice-routing checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
