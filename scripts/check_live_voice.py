"""Paid, short ES/EN/CA speech roundtrip; no patient data or submissions.

Run explicitly: python scripts/check_live_voice.py --stt elevenlabs
This checks transport/transcription, not subjective accent or booking accuracy.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.voice.stt import DeepgramTranscriber, ElevenLabsTranscriber
from src.voice.tts import ElevenLabsSynthesizer

SAMPLES = {
    'en': ('Hello, I need to move my appointment from Monday to Thursday.', ('thursday',)),
    'es': ('Hola, quiero cambiar mi cita del lunes al jueves.', ('jueves',)),
    'ca': ('Bon dia, voldria canviar la visita de dilluns a dijous, si us plau.', ('dijous',)),
}


async def run(provider: str, languages: list[str]) -> int:
    failed = 0
    synth = ElevenLabsSynthesizer()
    try:
        for language, (text, required) in SAMPLES.items():
            if language not in languages:
                continue
            finals, errors = [], []
            async def partial(_text): pass
            async def final(text, code): finals.append((text, code))
            async def notice(kind, payload):
                if 'error' in kind or 'stopped' in kind:
                    errors.append(kind)
            cls = ElevenLabsTranscriber if provider == 'elevenlabs' else DeepgramTranscriber
            transcriber = cls(partial, final, on_notice=notice)
            try:
                audio = b''.join([c async for c in synth.stream(text, language)])
                await transcriber.start()
                for offset in range(0, len(audio), 160):
                    await transcriber.push(audio[offset:offset+160])
                    await asyncio.sleep(.02)
                for _ in range(130):
                    await transcriber.push(b'\xff'*160)
                    await asyncio.sleep(.02)
            finally:
                await transcriber.finish()
            heard = ' '.join(t for t, _ in finals).lower()
            ok = all(word in heard for word in required) and not errors
            ok = ok and len(finals) == len(set(t for t, _ in finals))
            failed += not ok
            print(language, 'PASS' if ok else 'FAIL', 'transcripts=', finals, 'errors=', errors, flush=True)
    finally:
        await synth.aclose()
    return int(bool(failed))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stt', choices=['deepgram', 'elevenlabs'], required=True)
    parser.add_argument('--language', choices=['en', 'es', 'ca'], action='append')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.stt, args.language or list(SAMPLES))))
