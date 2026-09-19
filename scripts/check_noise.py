from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import re
import struct
import sys
import time
import unicodedata
import wave
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.voice.audio import FRAME_MS, SAMPLE_RATE, frames, pcm16_to_ulaw, resample_pcm16, rms, silence

FULL_SCALE = 32768
REFERENCE_DBFS = -20
NOISE_PEAK_DBFS = -6
MAX_AUDIO_S = 60


def samples(pcm: bytes) -> tuple[int, ...]:
    if not pcm or len(pcm) % 2:
        raise ValueError("audio must contain complete 16-bit samples")
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)


def pack(values) -> bytes:
    values = list(values)
    return struct.pack(f"<{len(values)}h", *values)


def load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("fixtures must be uncompressed 16-bit mono WAVs")
        if not 0 < handle.getnframes() <= handle.getframerate() * MAX_AUDIO_S:
            raise ValueError(f"fixtures must contain between 0 and {MAX_AUDIO_S} seconds of audio")
        return resample_pcm16(handle.readframes(handle.getnframes()), handle.getframerate())


def scale(pcm: bytes, dbfs: float, peak: int) -> tuple[bytes, int]:
    values = samples(pcm)
    level = math.sqrt(sum(value * value for value in values) / len(values))
    if not level:
        raise ValueError("a calibration fixture cannot be silent")
    factor = FULL_SCALE * 10 ** (dbfs / 20) / level
    scaled = [round(value * factor) for value in values]
    limited = sum(abs(value) > peak for value in scaled)
    return pack(max(-peak, min(peak, value)) for value in scaled), limited


def noise_bed(noise: bytes, length: int, snr: float) -> tuple[bytes, int]:
    samples(noise)
    if not math.isfinite(snr) or not -10 <= snr <= 40:
        raise ValueError("SNR must be finite and between -10 and 40 dB")
    tiled = (noise * ((length + len(noise) - 1) // len(noise)))[:length]
    return scale(tiled, REFERENCE_DBFS - snr, round(FULL_SCALE * 10 ** (NOISE_PEAK_DBFS / 20)))


def calibrate(speech: bytes, noise: bytes, snr: float) -> tuple[bytes, bytes, bytes, dict]:
    clean, speech_limited = scale(speech, REFERENCE_DBFS, FULL_SCALE - 1)
    bed, noise_limited = noise_bed(noise, len(clean), snr)
    summed = [s + n for s, n in zip(samples(clean), samples(bed))]
    clipped = sum(value < -FULL_SCALE or value >= FULL_SCALE for value in summed)
    mixed = pack(max(-FULL_SCALE, min(FULL_SCALE - 1, value)) for value in summed)
    return clean, bed, mixed, {
        "requested_snr_db": snr,
        "effective_snr_db": round(20 * math.log10(rms(clean) / rms(bed)), 3),
        "speech_rms_dbfs": round(20 * math.log10(rms(clean) / FULL_SCALE), 3),
        "noise_rms_dbfs": round(20 * math.log10(rms(bed) / FULL_SCALE), 3),
        "speech_limited_samples": speech_limited,
        "noise_limited_samples": noise_limited,
        "mix_clipped_samples": clipped,
    }


def words(text: str) -> list[str]:
    return re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())


def word_error_rate(reference: str, transcript: str) -> float:
    expected, heard = words(reference), words(transcript)
    if not expected:
        raise ValueError("reference text must contain words")
    previous = list(range(len(heard) + 1))
    for i, left in enumerate(expected, 1):
        current = [i]
        for j, right in enumerate(heard, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (left != right)))
        previous = current
    return previous[-1] / len(expected)


def make_trials(speech: bytes, noises: dict[str, bytes], snrs: list[float], noise_seconds: float) -> list[dict]:
    if not math.isfinite(noise_seconds) or not 0 < noise_seconds <= MAX_AUDIO_S:
        raise ValueError(f"noise-only duration must be between 0 and {MAX_AUDIO_S} seconds")
    clean, limited = scale(speech, REFERENCE_DBFS, FULL_SCALE - 1)
    trials = [{"name": "clean", "kind": "clean", "audio": pcm16_to_ulaw(clean),
               "calibration": {"speech_limited_samples": limited}}]
    for texture, noise in noises.items():
        for snr in snrs:
            _, _, mixed, stats = calibrate(speech, noise, snr)
            bed, _ = noise_bed(noise, max(1, round(noise_seconds * SAMPLE_RATE)) * 2, snr)
            for kind, pcm in (("noise_only", bed), ("mixed", mixed)):
                trials.append({"name": f"{texture}/{snr:g}dB/{kind}", "kind": kind,
                               "audio": pcm16_to_ulaw(pcm),
                               "calibration": stats if kind == "mixed" else {
                                   "requested_snr_db": snr,
                                   "noise_rms_dbfs": round(20 * math.log10(rms(bed) / FULL_SCALE), 3),
                               }})
    return trials


class ProbeStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def record(self, _call_id: str, kind: str, payload: dict) -> None:
        self.events.append({"kind": kind, "payload": payload})


async def run_trial(audio: bytes, agent_text: str, *, factory=None,
                    tail_s: float = 2, timeout_s: float = 90) -> dict[str, Any]:
    from src.config import settings

    original_db_path = settings.db_path
    object.__setattr__(settings, "db_path", ":memory:")
    try:
        from src.telephony.session import CallSession, MIN_SPEAKING_MS_BEFORE_BARGE_IN
    finally:
        object.__setattr__(settings, "db_path", original_db_path)
    from src.agent.llm import LLMClient
    from src.domain.catalog import Catalog
    from src.voice.stt import build_transcriber, keyterms_for

    store = ProbeStore()
    transcripts: list[str] = []
    interrupts: list[float] = []
    errors: list[str] = []
    started = time.monotonic()

    async def send(message: dict) -> None:
        if message.get("event") == "clear":
            interrupts.append((time.monotonic() - started) * 1000)

    catalog = Catalog.load()
    llm = LLMClient()
    session = CallSession("noise-probe", "noise-probe", None, send, catalog, store, llm,
                          text_mode=True, dry_run=True)
    session._closed = True
    session._frozen = True
    session._current_text = agent_text
    session._last_spoken = agent_text
    session.agent.note_agent_line(agent_text)

    async def on_final(text: str, language: str | None) -> None:
        transcripts.append(text)
        await session._on_final(text, language)

    async def on_notice(kind: str, payload: dict) -> None:
        if "error" in kind or "stopped" in kind:
            errors.append(kind)

    transcriber = None
    try:
        transcriber = (factory or build_transcriber)(
            session._on_partial, on_final, session._on_speech_started, on_notice,
            keyterms=keyterms_for(catalog),
        )
        session.transcriber = transcriber

        async def stream() -> None:
            nonlocal started
            await transcriber.start()
            started = time.monotonic()
            session._speaking_since = started - MIN_SPEAKING_MS_BEFORE_BARGE_IN / 1000
            tick = started
            for chunk in frames(audio + silence(round(tail_s * 1000))):
                await transcriber.push(chunk)
                if getattr(transcriber, "_closed", False):
                    raise ConnectionError("transcriber closed while streaming")
                tick += FRAME_MS / 1000
                await asyncio.sleep(max(0, tick - time.monotonic()))

        await asyncio.wait_for(stream(), timeout=timeout_s)
    except Exception as exc:
        errors.append(type(exc).__name__)
    finally:
        try:
            if transcriber is not None:
                await asyncio.wait_for(transcriber.finish(), timeout=5)
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            session._disarm_silence()
            await session.client.aclose()
            await llm.aclose()

    errors.extend(event["payload"].get("where", "session_error")
                  for event in store.events if event["kind"] == "error")
    return {"transcript": " ".join(transcripts), "interruptions": len(interrupts),
            "first_interrupt_ms": round(interrupts[0]) if interrupts else None,
            "partials": sum(event["kind"] == "stt_partial" for event in store.events),
            "errors": sorted(set(errors))}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Calibrate local noise fixtures; --live additionally spends STT quota in an isolated probe.",
        epilog="No dashboard calls, clinic submissions, LLM or TTS. Barge-in uses simulated ongoing agent speech, "
               "not real playback. WER is lexical (digits are not expanded). Use consented/test speech only. "
               "RMS calibration includes all samples; trim long leading/trailing silence from speech fixtures.",
    )
    result.add_argument("--speech", type=Path, required=True, help="clean 16-bit mono WAV, at most 60 seconds")
    result.add_argument("--reference", required=True, help="exact words in the speech fixture")
    result.add_argument("--noise", action="append", required=True, metavar="LABEL=FILE.wav",
                        help="repeat for street, television, room, car (supply your own fixtures)")
    result.add_argument("--snr", type=float, nargs="+", default=[5], help="reference SNR in dB; default 5")
    result.add_argument("--noise-seconds", type=float, default=4, help="duration of each separate noise-only trial")
    result.add_argument("--live", action="store_true", help="opt in to paid STT; default only checks calibration")
    result.add_argument("--max-wer", type=float, default=0.25, help="local acceptance threshold, not a Prosper score")
    result.add_argument("--agent-text", default="Let me check the available appointments for you.",
                        help="simulated agent utterance used by the production echo filter")
    result.add_argument("--report", type=Path, help="create a NEW JSON report; never overwrite an existing file")
    result.add_argument("--include-transcripts", action="store_true", help="include sensitive STT text in the report")
    return result


async def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        word_error_rate(args.reference, "")
        if not math.isfinite(args.max_wer) or not 0 <= args.max_wer <= 1:
            raise ValueError("--max-wer must be between 0 and 1")
        if not words(args.agent_text):
            raise ValueError("--agent-text must contain words")
        if args.report and (args.report.exists() or not args.report.parent.is_dir()):
            raise ValueError("--report needs an existing parent directory and a new filename")
        speech = load_wav(args.speech)
        noises = {}
        for item in args.noise:
            label, sep, path = item.partition("=")
            if not sep or not re.fullmatch(r"[a-zA-Z0-9_-]+", label) or label in noises:
                raise ValueError("each --noise must have a unique LABEL=FILE.wav")
            noises[label] = load_wav(Path(path))
        trials = make_trials(speech, noises, args.snr, args.noise_seconds)
    except (OSError, ValueError, wave.Error, EOFError) as exc:
        cli.error(str(exc))

    provider = None
    if args.live:
        from src.config import settings
        if settings.stt_provider not in {"deepgram", "elevenlabs"}:
            cli.error("live calibration requires streaming STT (deepgram or elevenlabs)")
        key = settings.deepgram_api_key if settings.stt_provider == "deepgram" else settings.elevenlabs_api_key
        if not key:
            cli.error(f"missing key for configured STT provider {settings.stt_provider}; no fallback is used")
        if not settings.barge_in:
            cli.error("barge-in is disabled; enable it in an isolated test environment before measuring it")
        provider = {"name": settings.stt_provider, "model": settings.stt_model_label,
                    "filter_background": settings.stt_filter_background,
                    "endpointing_ms": settings.stt_endpointing_ms,
                    "utterance_end_ms": settings.stt_utterance_end_ms}
        logging.getLogger("socketwizard").disabled = True

    report = {"mode": "live" if args.live else "calibration_only", "provider": provider,
              "reference_sha256": hashlib.sha256(args.reference.encode()).hexdigest(),
              "speech_pcm_sha256": hashlib.sha256(speech).hexdigest(),
              "noise_pcm_sha256": {name: hashlib.sha256(pcm).hexdigest() for name, pcm in noises.items()},
              "max_wer": args.max_wer, "trials": [],
              "limitations": ["Acoustic probe, not an end-to-end booking or official scored evaluation.",
                              "Simulated agent speech already past grace period; at most one interruption per trial.",
                              "Interrupt latency is measured from fixture start, including any leading silence.",
                              "Calibration levels are measured before mu-law encoding."]}
    duration = sum(len(t["audio"]) / SAMPLE_RATE + 2 for t in trials)
    print(f"{len(trials)} trials; {duration:.1f}s planned STT input including silence tails.")
    print("LIVE STT ONLY; isolated from dashboard and clinic." if args.live else
          "OFFLINE calibration only; no recognition or barge-in claims. Add --live to spend STT quota.")
    failed = False
    for trial in trials:
        row = {key: value for key, value in trial.items() if key != "audio"}
        row["duration_s"] = round(len(trial["audio"]) / SAMPLE_RATE, 3)
        row["ulaw_sha256"] = hashlib.sha256(trial["audio"]).hexdigest()
        row["status"] = "not_run"
        if args.live:
            result = await run_trial(trial["audio"], args.agent_text)
            transcript = result.pop("transcript")
            row.update(result)
            if args.include_transcripts:
                row["transcript"] = transcript
            if trial["kind"] == "noise_only":
                row["background_words"] = len(words(transcript))
                ok = not row["interruptions"] and not row["background_words"]
            else:
                row["wer"] = word_error_rate(args.reference, transcript)
                ok = row["wer"] <= args.max_wer and row["interruptions"] == 1
            row["status"] = "pass" if ok and not row["errors"] else "fail"
            failed |= row["status"] == "fail"
        report["trials"].append(row)
        print(json.dumps(row, ensure_ascii=True))
        if row.get("errors"):
            print("Stopping after a provider/transport failure; remaining trials were not run.")
            break
    report["planned_trials"] = len(trials)
    report["completed_trials"] = len(report["trials"])
    if args.report:
        with args.report.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
    return int(failed)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
