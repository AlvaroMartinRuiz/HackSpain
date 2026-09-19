"""Per-call audio, so the console can play back what actually hit the line."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from src.config import DATA_DIR
from src.voice.audio import ulaw_to_wav

RECORDINGS_DIR = Path(DATA_DIR) / "recordings"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
TRACKS = ("inbound", "outbound")


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


def available(call_id: str) -> dict[str, bool]:
    try:
        folder = _folder(call_id)
    except ValueError:
        return {track: False for track in TRACKS}
    return {track: (folder / f"{track}.wav").is_file() for track in TRACKS}


def wav_path(call_id: str, track: str) -> Optional[Path]:
    if track not in TRACKS:
        return None
    try:
        path = _folder(call_id) / f"{track}.wav"
    except ValueError:
        return None
    return path if path.is_file() else None
