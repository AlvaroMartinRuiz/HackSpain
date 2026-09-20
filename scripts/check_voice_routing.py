"""Offline checks for per-call Spanish, Catalan and English voice routing."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings  # noqa: E402
from src.agent.brain import Agent  # noqa: E402
from src.voice.language import decide_language, should_apply_language  # noqa: E402
from src.voice.stt import DeepgramTranscriber, ElevenLabsTranscriber  # noqa: E402
from src.voice.tts import (  # noqa: E402
    deepgram_model_for_language,
    elevenlabs_model_for_language,
    elevenlabs_voice_for_language,
)


async def _noop(*_args) -> None:
    return None


async def _low_confidence_is_preserved() -> bool:
    finals: list[str] = []
    notices: list[str] = []

    async def on_final(text: str, _language: str | None) -> None:
        finals.append(text)

    async def on_notice(kind: str, _payload: dict) -> None:
        notices.append(kind)

    transcriber = DeepgramTranscriber(_noop, on_final, on_notice=on_notice)
    await transcriber._handle({
        "type": "Results",
        "is_final": True,
        "speech_final": True,
        "channel": {
            "alternatives": [{
                "transcript": "television noise",
                "confidence": 0.1,
                "languages": ["en"],
            }],
        },
    })
    return finals == ["television noise"] and notices == ["stt_low_confidence"]


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
        ("lone hello keeps the current language", "Hello?", None, "es"),
        ("Spanish hora is not Catalan", "Necesito hora por la mañana, por favor.", None, "es"),
    ]
    for label, text, hint, expected in samples:
        total += 1
        decision = decide_language(text, hint)
        passed += check(label, decision.code == expected,
                        f"{decision.code} {decision.confidence:.2f} via {decision.source}")

    name_decision = decide_language(
        "José Martínez", "es", current="en", established=True
    )
    total += 1
    passed += check(
        "Spanish name does not switch an English call",
        name_decision.code == "en",
        f"{name_decision.code} via {name_decision.source}",
    )
    total += 1
    passed += check(
        "Scribe spa maps to Spanish",
        decide_language("José Martínez", "spa", current="es").code == "es",
    )
    named_sentence = decide_language(
        "Hello, it's for José Martínez", "es", current="en", established=True
    )
    total += 1
    passed += check(
        "Spanish name inside English stays English",
        named_sentence.code == "en",
        f"{named_sentence.code} via {named_sentence.source}",
    )
    explicit_spanish = decide_language(
        "Hola, necesito una cita por la mañana, por favor.", "es", current="en"
    )
    total += 1
    passed += check(
        "Clear Spanish sentence may switch an English call",
        should_apply_language("en", True, explicit_spanish),
        f"{explicit_spanish.code} via {explicit_spanish.source}",
    )

    from src.agent.brain import _liveness_response, _ready_to_speak

    total += 1
    passed += check("Hello is not a line check", _liveness_response("Hello?", "es") is None)
    total += 1
    passed += check(
        "Are you there is a line check in English",
        (_liveness_response("Are you still there?", "es") or "").startswith("Yes"),
    )
    total += 1
    passed += check(
        "Hola me oyes is a line check in Spanish",
        (_liveness_response("Hola, ¿me oyes?", "es") or "").startswith("Sí"),
    )
    total += 1
    passed += check("Short question speaks now", _ready_to_speak(["¿Hablo con Ella Smith?"]))
    total += 1
    passed += check("Filler waits", not _ready_to_speak(["Thank you."]))

    total += 1
    passed += check(
        "Catalan STT locks to ca",
        parse_qs(urlparse(DeepgramTranscriber(_noop, _noop)._url("ca")).query)["language"] == ["ca"],
    )

    qs = parse_qs(urlparse(DeepgramTranscriber(_noop, _noop)._url()).query)
    total += 1
    passed += check("numerals are on", qs.get("numerals") == ["true"])
    total += 1
    passed += check("endpointing follows configuration", qs.get("endpointing") == [str(settings.stt_endpointing_ms)])
    total += 1
    passed += check(
        "low-confidence text is retained with a diagnostic notice",
        asyncio.run(_low_confidence_is_preserved()),
    )

    scribe = parse_qs(urlparse(ElevenLabsTranscriber(_noop, _noop)._url()).query)
    total += 1
    passed += check("Scribe listens to µ-law 8 kHz", scribe.get("audio_format") == ["ulaw_8000"])
    total += 1
    passed += check("Scribe commits on VAD", scribe.get("commit_strategy") == ["vad"])
    total += 1
    passed += check(
        "Catalan Scribe lock",
        parse_qs(urlparse(ElevenLabsTranscriber(_noop, _noop)._url("ca")).query).get("language_code")
        == ["ca"],
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
    passed += check(
        "Spanish ElevenLabs voice is not the English voice",
        elevenlabs_voice_for_language("es") != elevenlabs_voice_for_language("en"),
        f"es={elevenlabs_voice_for_language('es')[:8]} en={elevenlabs_voice_for_language('en')[:8]}",
    )
    total += 1
    passed += check(
        "English ElevenLabs voice is Sarah",
        elevenlabs_voice_for_language("en") == settings.elevenlabs_voice_id_en,
    )

    total += 1
    passed += check(
        "internal deadline leaves harness cleanup room",
        settings.call_hard_limit_s <= 145,
        str(settings.call_hard_limit_s),
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

    from src.agent import phrases as voice_phrases

    total += 1
    passed += check(
        "model errors are not phrased as deafness",
        voice_phrases.MODEL_DOWN["es"] != voice_phrases.RETRY["es"]
        and "oído" not in voice_phrases.MODEL_DOWN["es"].lower(),
    )

    print(f"\n{passed}/{total} voice-routing checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
