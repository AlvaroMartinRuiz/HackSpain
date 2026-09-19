"""8 kHz µ-law, which is all a phone line carries.

``audioop`` does the work where it exists; the pure-Python path keeps this
working on 3.13, where it was removed from the standard library.
"""

from __future__ import annotations

import io
import struct
import wave
from typing import Iterator, Optional

try:  # Python <= 3.12
    import audioop  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - 3.13+
    audioop = None  # type: ignore

SAMPLE_RATE = 8000
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 160
FRAME_BYTES = SAMPLES_PER_FRAME  # one byte per µ-law sample
SILENCE_BYTE = b"\xff"
BIAS = 0x84
CLIP = 32635

_ULAW_TO_LINEAR: Optional[list[int]] = None


def _linear_to_ulaw_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    sample = min(sample, CLIP) + BIAS

    exponent = 7
    mask = 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _ulaw_table() -> list[int]:
    global _ULAW_TO_LINEAR
    if _ULAW_TO_LINEAR is None:
        table: list[int] = []
        for byte in range(256):
            value = ~byte & 0xFF
            sign = value & 0x80
            exponent = (value >> 4) & 0x07
            mantissa = value & 0x0F
            magnitude = ((mantissa << 3) + BIAS) << exponent
            magnitude -= BIAS
            table.append(-magnitude if sign else magnitude)
        _ULAW_TO_LINEAR = table
    return _ULAW_TO_LINEAR


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    if audioop is not None:
        return audioop.lin2ulaw(pcm, 2)
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    return bytes(_linear_to_ulaw_sample(sample) for sample in samples)


def ulaw_to_pcm16(ulaw: bytes) -> bytes:
    if audioop is not None:
        return audioop.ulaw2lin(ulaw, 2)
    table = _ulaw_table()
    return struct.pack(f"<{len(ulaw)}h", *(table[byte] for byte in ulaw))


def resample_pcm16(pcm: bytes, source_rate: int, target_rate: int = SAMPLE_RATE) -> bytes:
    """Rate-convert 16-bit mono PCM."""
    if source_rate == target_rate or not pcm:
        return pcm
    if audioop is not None:
        converted, _ = audioop.ratecv(pcm, 2, 1, source_rate, target_rate, None)
        return converted

    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    ratio = target_rate / source_rate
    out_length = int(len(samples) * ratio)
    out: list[int] = []
    for index in range(out_length):
        position = index / ratio
        left = int(position)
        right = min(left + 1, len(samples) - 1)
        weight = position - left
        out.append(int(samples[left] * (1 - weight) + samples[right] * weight))
    return struct.pack(f"<{len(out)}h", *out)


def frames(payload: bytes, size: int = FRAME_BYTES) -> Iterator[bytes]:
    """Split a µ-law buffer into 20 ms frames, padding the last one."""
    for start in range(0, len(payload), size):
        chunk = payload[start : start + size]
        if len(chunk) < size:
            chunk = chunk + SILENCE_BYTE * (size - len(chunk))
        yield chunk


def silence(duration_ms: int) -> bytes:
    return SILENCE_BYTE * (SAMPLE_RATE * duration_ms // 1000)


def rms(pcm: bytes) -> float:
    """Loudness of a 16-bit PCM buffer, for a plain energy gate."""
    if not pcm:
        return 0.0
    if audioop is not None:
        return float(audioop.rms(pcm, 2))
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    return (sum(sample * sample for sample in samples) / len(samples)) ** 0.5


def ulaw_to_wav(ulaw: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap µ-law audio as a 16-bit WAV, which is what transcribers accept."""
    pcm = ulaw_to_pcm16(ulaw)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()
