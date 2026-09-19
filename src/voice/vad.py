"""An energy gate, for when the transcriber does not do endpointing itself."""

from __future__ import annotations

from dataclasses import dataclass

from src.voice.audio import FRAME_MS, rms, ulaw_to_pcm16


@dataclass
class EnergyVAD:
    threshold: float = 550.0
    start_frames: int = 3
    end_frames: int = 35  # roughly 700 ms of quiet closes a turn

    speaking: bool = False
    _loud: int = 0
    _quiet: int = 0

    def feed(self, ulaw_frame: bytes) -> str:
        """Returns "start", "end" or "" for one 20 ms frame."""
        level = rms(ulaw_to_pcm16(ulaw_frame))
        if level >= self.threshold:
            self._loud += 1
            self._quiet = 0
            if not self.speaking and self._loud >= self.start_frames:
                self.speaking = True
                return "start"
        else:
            self._quiet += 1
            self._loud = 0
            if self.speaking and self._quiet >= self.end_frames:
                self.speaking = False
                return "end"
        return ""

    @property
    def trailing_silence_ms(self) -> int:
        return self._quiet * FRAME_MS
