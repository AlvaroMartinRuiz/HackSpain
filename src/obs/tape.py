"""Per-call audio, so the console can play back what actually hit the line."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from src.config import DATA_DIR
from src.voice import audio as voice_audio
from src.voice.audio import mix_ulaw, pcm16_to_ulaw, ulaw_to_wav

RECORDINGS_DIR = Path(DATA_DIR) / "recordings"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
# Side tracks are kept for forensics; the console plays the mixed conversation.
SIDE_TRACKS = ("inbound", "outbound")
TRACKS = (*SIDE_TRACKS, "conversation")


def _folder(call_id: str) -> Path:
    if not _SAFE_ID.match(call_id):
        raise ValueError("unsafe call id")
    return RECORDINGS_DIR / call_id


def save(call_id: str, track: str, ulaw: bytes) -> None:
    if track not in TRACKS or not ulaw:
        return
    folder = _folder(call_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{track}.wav").write_bytes(ulaw_to_wav(ulaw))


def save_call(call_id: str, inbound: bytes, outbound: bytes) -> None:
    """Write the side tapes and the mixed conversation the console plays."""
    if not inbound and not outbound:
        return
    length = max(len(inbound), len(outbound))
    if length <= 0:
        return
    inbound = voice_audio.pad_ulaw(inbound, length)
    outbound = voice_audio.pad_ulaw(outbound, length)
    folder = _folder(call_id)
    folder.mkdir(parents=True, exist_ok=True)
    if inbound:
        (folder / "inbound.wav").write_bytes(ulaw_to_wav(inbound))
    if outbound:
        (folder / "outbound.wav").write_bytes(ulaw_to_wav(outbound))
    mixed = mix_ulaw(inbound, outbound)
    if mixed:
        (folder / "conversation.wav").write_bytes(ulaw_to_wav(mixed))


def available(call_id: str) -> dict[str, bool]:
    """What the console can play. Prefers the mixed conversation track."""
    try:
        folder = _folder(call_id)
    except ValueError:
        return {"conversation": False}
    conversation = (folder / "conversation.wav").is_file()
    if not conversation:
        # Older tapes only have the sides — still playable once mixed on demand.
        conversation = any((folder / f"{track}.wav").is_file() for track in SIDE_TRACKS)
    return {"conversation": conversation}


def wav_path(call_id: str, track: str) -> Optional[Path]:
    if track not in TRACKS:
        return None
    try:
        folder = _folder(call_id)
    except ValueError:
        return None

    if track == "conversation":
        path = folder / "conversation.wav"
        if path.is_file():
            return path
        return _ensure_conversation(call_id)

    path = folder / f"{track}.wav"
    return path if path.is_file() else None


def _ensure_conversation(call_id: str) -> Optional[Path]:
    """Build conversation.wav from the side tapes of an older recording."""
    try:
        folder = _folder(call_id)
    except ValueError:
        return None
    sides: list[bytes] = []
    for name in SIDE_TRACKS:
        path = folder / f"{name}.wav"
        if not path.is_file():
            continue
        try:
            sides.append(_wav_to_ulaw(path))
        except (OSError, ValueError):
            continue
    if not sides:
        return None
    mixed = mix_ulaw(*sides)
    if not mixed:
        return None
    out = folder / "conversation.wav"
    out.write_bytes(ulaw_to_wav(mixed))
    return out


def _wav_to_ulaw(path: Path) -> bytes:
    """Read a 16-bit mono WAV back to µ-law for remixing."""
    import wave

    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("expected 16-bit mono WAV")
        pcm = handle.readframes(handle.getnframes())
    return pcm16_to_ulaw(pcm)
