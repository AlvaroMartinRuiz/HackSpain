"""Pipecat Smart Turn: do not start a reply until the caller has finished.

Deepgram still transcribes. Clinic tools, the LLM and ElevenLabs stay ours.
This module only answers "have they stopped, or are they still going?".
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from src.config import settings
from src.voice.audio import SAMPLE_RATE, resample_pcm16, ulaw_to_pcm16
from src.voice.vad import EnergyVAD

logger = logging.getLogger("socketwizard.turns")

SMART_TURN_RATE = 16000
_INFER_LOCK = threading.Lock()
_LOAD_LOCK = threading.Lock()
_MODEL: Any = None
_READY = False
_ERROR = ""


def available() -> bool:
    return bool(settings.smart_turn and _READY)


def status() -> str:
    if not settings.smart_turn:
        return "off"
    if _READY:
        return "ready"
    return _ERROR or "warming"


def warm() -> bool:
    """Load the ONNX model once at start-up so the first call is not the download."""
    return _ensure_model() is not None


def _ensure_model() -> Any:
    global _MODEL, _READY, _ERROR
    if _MODEL is not None:
        return _MODEL
    if not settings.smart_turn:
        _ERROR = "off"
        return None
    with _LOAD_LOCK:
        if _MODEL is not None:
            return _MODEL
        if not settings.smart_turn:
            _ERROR = "off"
            return None
        try:
            from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

            proto = LocalSmartTurnAnalyzerV3(sample_rate=SMART_TURN_RATE, cpu_count=1)
            _MODEL = (proto._session, proto._feature_extractor)
            _READY = True
            logger.info("pipecat smart-turn v3 ready")
            return _MODEL
        except Exception as exc:
            _ERROR = f"{type(exc).__name__}: {exc}"
            _READY = False
            logger.warning("pipecat smart-turn unavailable: %s", exc)
            return None


def _make_analyzer() -> Any:
    from pipecat.audio.turn.smart_turn.base_smart_turn import BaseSmartTurn, SmartTurnParams
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

    model = _ensure_model()
    if model is None:
        return None

    class ClinicSmartTurn(LocalSmartTurnAnalyzerV3):
        def __init__(self) -> None:
            params = SmartTurnParams(
                stop_secs=settings.smart_turn_stop_secs,
                max_duration_secs=12.0,
                pre_speech_ms=300.0,
            )
            BaseSmartTurn.__init__(self, sample_rate=SMART_TURN_RATE, params=params)
            self._session, self._feature_extractor = model
            self._log_data = False
            self.set_sample_rate(SMART_TURN_RATE)

        def _predict_endpoint(self, audio_array):  # type: ignore[override]
            with _INFER_LOCK:
                return LocalSmartTurnAnalyzerV3._predict_endpoint(self, audio_array)

    return ClinicSmartTurn()


def _make_silero() -> Any:
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams

    vad = SileroVADAnalyzer(
        sample_rate=SAMPLE_RATE,
        params=VADParams(
            confidence=0.5,
            start_secs=0.15,
            stop_secs=0.2,
            min_volume=0.35,
        ),
    )
    vad.set_sample_rate(SAMPLE_RATE)
    return vad


class TurnGate:
    """Per-call listener: VAD for 'they started', Smart Turn for 'they finished'."""

    def __init__(self) -> None:
        self._analyzer = _make_analyzer()
        self._energy = EnergyVAD()
        self._silero = None
        if self._analyzer is not None:
            try:
                self._silero = _make_silero()
            except Exception as exc:
                logger.info("silero vad unavailable, energy gate only: %s", exc)
        self._speaking = False
        self.turn_over = False
        self.last_probability: Optional[float] = None

    @property
    def enabled(self) -> bool:
        return self._analyzer is not None

    @property
    def speaking(self) -> bool:
        return self._speaking

    async def feed_ulaw(self, ulaw: bytes) -> str:
        """Returns 'start', 'pause', 'complete' or '' for one 20 ms frame."""
        if self._analyzer is None:
            return ""

        pcm8 = ulaw_to_pcm16(ulaw)
        pcm16 = resample_pcm16(pcm8, SAMPLE_RATE, SMART_TURN_RATE)
        was = self._speaking
        became_quiet = False
        became_speech = False

        if self._silero is not None:
            from pipecat.audio.vad.vad_analyzer import VADState

            state = await self._silero.analyze_audio(pcm8)
            self._speaking = state in {VADState.STARTING, VADState.SPEAKING, VADState.STOPPING}
            became_quiet = was and state == VADState.QUIET
            became_speech = (not was) and state == VADState.SPEAKING
        else:
            event = self._energy.feed(ulaw)
            self._speaking = self._energy.speaking
            became_quiet = event == "end"
            became_speech = event == "start"

        result = ""
        if became_speech:
            self.turn_over = False
            result = "start"

        from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState

        timeout = self._analyzer.append_audio(pcm16, self._speaking)
        if timeout == EndOfTurnState.COMPLETE:
            self.turn_over = True
            return "complete"
        if became_quiet:
            return "pause"
        return result

    async def decide(self) -> bool:
        """Ask the model whether this pause is the end of their turn."""
        if self._analyzer is None:
            return True
        from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState

        state, metrics = await self._analyzer.analyze_end_of_turn()
        if metrics is not None:
            self.last_probability = getattr(metrics, "probability", None)
        complete = state == EndOfTurnState.COMPLETE
        self.turn_over = complete
        return complete

    def reset(self) -> None:
        self.turn_over = False
        self.last_probability = None
        if self._analyzer is not None:
            self._analyzer.clear()
