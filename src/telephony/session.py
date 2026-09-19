"""One call, start to finish.

Everything a call owns lives on one of these: its own transcriber, its own
synthesiser, its own conversation and its own record. Nothing is shared
between sockets, which is what lets twenty of them run at once.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import Any, Awaitable, Callable, Optional

from src.agent import phrases
from src.agent.brain import Agent
from src.agent.llm import LLMClient
from src.config import settings
from src.domain.catalog import Catalog
from src.domain.engine import SchedulingEngine
from src.obs.store import CallStore
from src.obs import tape
from src.platform_api.client import PlatformClient, SubmitResult
from src.voice import audio, tts
from src.voice.language import decide_language, should_apply_language
from src.voice.stt import build_transcriber, keyterms_for
from src.voice.tts import Synthesizer, build_synthesizer
from src.voice.turns import TurnGate

Sender = Callable[[dict[str, Any]], Awaitable[None]]
Closer = Callable[[], Awaitable[None]]

# Deepgram's Nova-3 barge-in guidance: two words on an interim result. We stop
# ourselves when they talk; we never start a turn until Pipecat Smart Turn
# says they have finished.
MIN_BARGE_IN_WORDS = 2
MIN_SPEAKING_MS_BEFORE_BARGE_IN = 350
ECHO_OVERLAP = 0.75
PLAYBACK_LEAD_S = 0.20
# ~3 minutes of µ-law; the harness cuts the call before this anyway.
TAPE_CAP_BYTES = 8000 * 180

# Spent recovering the caller's last words once the socket is gone. The window
# closes 30 s after that and a retried submit can take 19 of them, so this stays
# small enough that the record is never what runs out of time.
RECOVERY_BUDGET_S = 6.0

# Grace for a turn still in flight when the hard limit lands, inside the ten
# seconds between our own limit and the harness cutting the call at three
# minutes.
DEADLINE_SETTLE_S = 5.0
# After the Spanish greeting, give a slow English caller time to start before
# we re-ask. Once they have spoken, wait a little longer between prompts.
SILENCE_OPENING_RETRY_S = 10.0
SILENCE_FIRST_PROMPT_S = 8.0
SILENCE_SECOND_PROMPT_S = 12.0
SILENCE_CLOSE_S = 18.0
COMPLETED_CLOSE_S = 15.0
# VAD often fires on room noise. If no transcript follows, keep waiting.
SPEECH_HOLD_S = 1.8


class CallSession:
    def __init__(
        self,
        call_id: str,
        stream_sid: str,
        from_number: Optional[str],
        send: Sender,
        catalog: Catalog,
        store: CallStore,
        llm: LLMClient,
        text_mode: bool = False,
        dry_run: bool = False,
        close: Optional[Closer] = None,
    ) -> None:
        self.call_id = call_id
        self.stream_sid = stream_sid
        self.from_number = from_number
        self.catalog = catalog
        self.store = store
        self._send = send
        self.text_mode = text_mode
        self.dry_run = dry_run
        self._close_wire = close

        self.client = PlatformClient(on_event=self._client_event)
        self.engine = SchedulingEngine(self.client, catalog)
        self.agent = Agent(self, llm)
        self.synthesizer: Optional[Synthesizer] = None if text_mode else build_synthesizer()
        self.transcriber = None if text_mode else build_transcriber(
            on_partial=self._on_partial,
            on_final=self._on_final,
            on_speech_started=self._on_speech_started,
            on_notice=self._on_stt_notice,
            keyterms=keyterms_for(catalog),
        )
        self._turn_gate = None if text_mode else TurnGate()
        self._turn_parts: list[str] = []
        self._last_turn_decide_at = 0.0
        self._last_part_at = 0.0
        self._turn_deadline_task: Optional[asyncio.Task] = None
        self._last_committed = ""

        self.seen_patients: list[dict[str, Any]] = []
        self.submissions: list[SubmitResult] = []
        self._submitted_keys: set[str] = set()

        self._say_queue: asyncio.Queue = asyncio.Queue()
        self._audio_queue: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._generation = 0
        self._speaking_since: Optional[float] = None
        self._current_text = ""
        self._last_spoken = ""
        self._current_sent = 0
        self._current_total = 0
        self._synthesizing = 0
        self._turn_lock = asyncio.Lock()
        self._pending_turn: Optional[str] = None
        # Two flags, not one: the door shuts to new turns only after the last
        # one has been recovered, but finalize still must not run twice.
        self._closing = False
        self._closed = False
        self._sealing = False
        self._frozen = False
        self.language = settings.default_language
        # Not in _tasks: it is cancelled and re-created on every turn.
        self._silence_task: Optional[asyncio.Task] = None
        self._silence_prompts = 0
        self.language_confidence = 0.0
        self.language_source = "default"
        self._language_established = False
        self._heard_caller = False
        self._started_at = time.monotonic()
        self._last_partial_at = 0.0
        self._last_speech_started_at = 0.0
        self._caller_speaking = False
        self._waiting_since: Optional[float] = None
        self._silence_prompts = 0
        self._last_repair_prompt_at = 0.0
        self._media_frames = 0
        self._media_bytes = 0
        self._media_by_track: dict[str, int] = {}
        self._tape: dict[str, bytearray] = {"inbound": bytearray(), "outbound": bytearray()}
        self._tape_saved = False

    # ---- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        if not self.text_mode:
            self._tasks = [
                asyncio.create_task(self._speaker_loop(), name=f"speaker:{self.call_id}"),
                asyncio.create_task(self._player_loop(), name=f"player:{self.call_id}"),
                asyncio.create_task(self._deadline_loop(), name=f"deadline:{self.call_id}"),
            ]
        await self.record("decision", {"stage": "greeting", "why": "call connected"})
        # Do not await the greeting here: the socket still has to read inbound
        # audio while the first sentence is synthesised.
        self._tasks.append(asyncio.create_task(self._greet(), name=f"greet:{self.call_id}"))
        if not self.text_mode:
            # The greeting needs no ears, so it plays while the transcriber
            # connects instead of after. Inbound frames wait in the socket until
            # this returns, so nothing the caller says is lost.
            assert self.transcriber is not None
            await self.transcriber.start()

    async def _greet(self) -> None:
        try:
            await self.agent.greet()
        except Exception as exc:
            await self.record("error", {"where": "greet", "detail": f"{type(exc).__name__}: {exc}"})

    async def on_media(self, payload: str, track: Optional[str] = None) -> None:
        if self.transcriber is None:
            return
        try:
            chunk = base64.b64decode(payload)
        except (ValueError, TypeError):
            return
        if not chunk:
            return
        # Prosper's "inbound" should be the patient, but the last practice
        # delivered 19 s of inbound and Deepgram heard silence — so we transcribe
        # every track and drop echoes of our own speech later.
        key = track or "inbound"
        self._media_by_track[key] = self._media_by_track.get(key, 0) + 1
        self._media_frames += 1
        self._media_bytes += len(chunk)
        self._pad_tape(key)
        buf = self._tape.setdefault(key, bytearray())
        if len(buf) < TAPE_CAP_BYTES:
            buf.extend(chunk)
        if self._media_frames == 1:
            await self.record("media_started", {
                "track": track, "frame_bytes": len(chunk),
            })
        await self.transcriber.push(chunk)
        if (
            key == "inbound"
            and self._turn_gate is not None
            and self._turn_gate.enabled
        ):
            event = await self._turn_gate.feed_ulaw(chunk)
            await self._on_turn_event(event)

    async def _on_stt_notice(self, kind: str, payload: dict[str, Any]) -> None:
        await self.record(kind, payload)
        if kind == "stt_utterance_end":
            if self._turn_gate is not None and self._turn_gate.enabled:
                await self._maybe_close_turn("utterance_end")
                return
            self._caller_speaking = False
            if (
                self._waiting_since is None
                and not self.is_speaking
                and not self._turn_lock.locked()
            ):
                self._waiting_since = time.monotonic()
            return
        if kind != "stt_low_confidence" or self._closed or self._closing:
            return
        if self._turn_parts:
            return
        self._caller_speaking = False
        now = time.monotonic()
        if (
            now - self._last_repair_prompt_at < 4.0
            or self.is_speaking
            or self._turn_lock.locked()
        ):
            return
        self._last_repair_prompt_at = now
        prompt = phrases.pick(phrases.RETRY, self.language)
        self.agent.note_agent_line(prompt)
        await self.say(prompt)

    def _tape_target_bytes(self) -> int:
        """How long the tape should be right now, in µ-law bytes at 8 kHz.

        Outbound only grows while the agent speaks, so without this pad the
        two sides of a call cannot be mixed into one conversation.
        """
        elapsed = max(0.0, time.monotonic() - self._started_at)
        return min(TAPE_CAP_BYTES, int(elapsed * audio.SAMPLE_RATE))

    def _pad_tape(self, track: str) -> None:
        buf = self._tape.setdefault(track, bytearray())
        target = self._tape_target_bytes()
        if len(buf) < target:
            buf.extend(audio.SILENCE_BYTE * (target - len(buf)))

    def _save_tape(self) -> None:
        if self._tape_saved:
            return
        self._tape_saved = True
        # Both sides up to the same wall-clock length before the mix, so a
        # quiet stretch of the agent is silence on the tape rather than a gap
        # that collapses the timeline.
        for track in ("inbound", "outbound"):
            self._pad_tape(track)
        try:
            tape.save_call(
                self.call_id,
                bytes(self._tape.get("inbound", b"")),
                bytes(self._tape.get("outbound", b"")),
            )
        except Exception:
            pass

    async def on_stop(self) -> None:
        self._save_tape()
        await self.record("media_stats", {
            "frames": self._media_frames,
            "bytes": self._media_bytes,
            "by_track": self._media_by_track,
            "deepgram_bytes": getattr(self.transcriber, "bytes_sent", None),
        })
        if self.transcriber is not None:
            await self.transcriber.finish()

    async def feed_text(self, text: str) -> None:
        """A caller turn typed instead of spoken, for rehearsal and evaluation."""
        await self._on_final(text, None)

    async def finalize(self, status: str = "finished") -> None:
        """Close the call, and never leave its record empty."""
        if self._closing:
            return
        self._closing = True
        self._disarm_silence()
        self._disarm_turn_deadline()

        await self._recover_last_turn()
        self._closed = True
        # From here only the safety-net NO_ACTION may be written. An in-flight
        # BOOK after that would leave [NO_ACTION, BOOK] and fail the case.
        self._sealing = True

        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        for task in self._tasks:
            if task is current:
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        await self._guarantee_submission()
        self._frozen = True
        self._save_tape()

        # STT is already closed: either on_stop drained it, or _recover_last_turn
        # did. A third finish() only retries CloseStream on a dead socket.
        if self.synthesizer is not None:
            await self.synthesizer.aclose()
        await self.client.aclose()

        self.store.close_call(self.call_id, status)
        await self.store.announce({"type": "call_ended", "call_id": self.call_id})

    async def _recover_last_turn(self) -> None:
        """Take back whatever the transcriber is still holding, before deciding.

        Deepgram sends its closing transcript after CloseStream, and when a
        socket drops without a `stop` that sentence is usually the caller's
        confirmation. Draining it here rather than after `_guarantee_submission`
        is what lets it still become a booking instead of arriving behind a
        NO_ACTION that has already gone out.
        """
        if self.transcriber is None:
            return
        try:
            await asyncio.wait_for(self._drain_and_settle(), timeout=RECOVERY_BUDGET_S)
        except (asyncio.TimeoutError, Exception):
            # Anything still outstanding is not worth the submission window.
            pass
        if self._turn_parts:
            await self._commit_turn()

    async def _drain_and_settle(self) -> None:
        assert self.transcriber is not None
        # finish() runs the recovered turn itself, unless one was already in
        # flight — in which case that turn picks it up and the lock is the wait.
        await self.transcriber.finish()
        await self._settled()

    async def _settled(self) -> None:
        """Return once no turn is being worked on."""
        async with self._turn_lock:
            pass

    async def _guarantee_submission(self) -> None:
        """Silence is always wrong, so something is always reported."""
        if any(result.accepted or result.duplicate for result in self.submissions):
            return
        leftover = self.agent.tools.completable_registration()
        if leftover:
            await self.record("decision", {
                "stage": "safety_net",
                "why": "the call ended with a complete registration still unsubmitted",
            })
            await self.submit("register", {"call_id": self.call_id, **leftover})
            if any(result.accepted or result.duplicate for result in self.submissions):
                return
        reason = self.agent.tools.fallback_reason()
        await self.record("decision", {
            "stage": "safety_net",
            "why": "the call ended with nothing on the record",
            "reason": reason,
        })
        await self.submit("no_action", {"call_id": self.call_id, "reason": reason})

    async def _deadline_loop(self) -> None:
        """Finish with enough room for draining and a worst-case submit retry."""
        try:
            await asyncio.sleep(settings.call_hard_limit_s)
        except asyncio.CancelledError:
            raise
        await self.record("error", {"where": "deadline", "detail": "hard call limit reached"})
        # A turn already running may still produce the real action, so give it
        # a moment rather than racing it to a timed-out NO_ACTION.
        try:
            await asyncio.wait_for(self._settled(), timeout=DEADLINE_SETTLE_S)
        except (asyncio.TimeoutError, Exception):
            pass
        await self.finalize("timed_out")
        await self._close_connection()

    async def _close_connection(self) -> None:
        if self._close_wire is None:
            return
        try:
            await self._close_wire()
        except Exception:
            pass

    # ---- speaking -----------------------------------------------------

    async def say(self, text: str, first: bool = False, language: Optional[str] = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        if _PLACEHOLDER.search(text):
            # The model filled a slot it did not have ("¿Hablo con [nombre del
            # paciente]?"). Saying it aloud is worse than saying nothing.
            await self.record("decision", {"stage": "placeholder_suppressed", "text": text})
            return
        spoken_language = language or self.language
        self._disarm_silence()
        # Logged when decided rather than when finished playing, so the console
        # shows the turn as the caller starts hearing it.
        await self.record("agent_said", {"text": text, "greeting": first, "language": spoken_language})
        self._last_spoken = text
        if not self.text_mode:
            await self._say_queue.put((self._generation, text, spoken_language))

    async def _speaker_loop(self) -> None:
        while True:
            generation, text, language = await self._say_queue.get()
            if generation != self._generation:
                continue
            # Counted, because between taking the text off the queue and the
            # first frame reaching the wire neither queue holds anything and the
            # agent would otherwise look idle for the length of a synthesis.
            self._synthesizing += 1
            try:
                await self._synthesize(generation, text, language)
            finally:
                self._synthesizing -= 1
                await self._audio_queue.put((generation, text, None))
            # A synthesis that failed outright never reaches _finish_speaking.
            self._arm_silence()

    async def _synthesize(self, generation: int, text: str, language: str) -> None:
        started = time.perf_counter()
        first_byte_ms: Optional[int] = None
        total = 0
        cached = tts.cached_audio(text, language)
        if cached:
            await self._audio_queue.put((generation, text, cached))
            await self.record("tts", {"text": text, "bytes": len(cached), "cached": True})
            return
        keep = [] if tts.is_fixed_line(text) else None
        # Barge-in during synthesis comes from is_speaking counting
        # _synthesizing. _speaking_since stays "audio is on the line": the
        # player keys the start of an utterance off it.
        try:
            async for chunk in self.synthesizer.stream(text, language):
                if generation != self._generation:
                    return
                if first_byte_ms is None:
                    first_byte_ms = int((time.perf_counter() - started) * 1000)
                total += len(chunk)
                if keep is not None:
                    keep.append(chunk)
                await self._audio_queue.put((generation, text, chunk))
        except Exception as exc:
            await self.record("error", {"where": "tts", "detail": str(exc)})
            return

        if keep:
            tts.remember_audio(text, language, b"".join(keep))
        await self.record("tts", {
            "text": text, "bytes": total, "first_byte_ms": first_byte_ms,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        })

    async def _player_loop(self) -> None:
        """Send 20 ms frames at the pace a phone line plays them."""
        playhead = time.monotonic()
        while True:
            generation, text, chunk = await self._audio_queue.get()
            if generation != self._generation:
                continue
            if chunk is None:
                if (
                    self._audio_queue.empty()
                    and self._say_queue.empty()
                    and self._synthesizing == 0
                ):
                    await self._finish_speaking(text)
                    playhead = max(playhead, time.monotonic())
                continue

            if self._speaking_since is None:
                self._speaking_since = time.monotonic()
                self._current_text = text
                self._current_sent = 0
                self._current_total = 0
                playhead = max(playhead, time.monotonic())
                await self.record("agent_speaking", {"text": text})
            elif text != self._current_text:
                self._current_text = text
                self._current_sent = 0
                self._current_total = 0

            self._current_total += len(chunk)
            # A playhead left behind by a pause would let the next frames out in
            # a burst, already delivered in full by the time the caller cuts in.
            playhead = max(playhead, time.monotonic())
            for frame in audio.frames(chunk):
                if generation != self._generation:
                    break
                now = time.monotonic()
                if playhead > now + PLAYBACK_LEAD_S:
                    await asyncio.sleep(playhead - now - PLAYBACK_LEAD_S)
                await self._send_media(frame)
                self._current_sent += len(frame)
                playhead += audio.FRAME_MS / 1000

    async def _finish_speaking(self, text: str) -> None:
        if self._speaking_since is None:
            return
        self._speaking_since = None
        # Cleared so a later interruption cannot attribute this turn's words to
        # the one being synthesised.
        self._current_text = ""
        self._current_sent = 0
        self._current_total = 0
        self._waiting_since = time.monotonic()
        await self.record("agent_turn_end", {"text": text})
        self._arm_silence()

    # ---- the caller goes quiet ------------------------------------------

    def _arm_silence(self) -> None:
        """Start listening for silence, if the agent has nothing more to say."""
        if (self.text_mode or self._closing or self._sealing or self.is_speaking
                or self._turn_lock.locked()
                or self._turn_parts
                or self._silence_prompts >= settings.silence_prompt_max):
            return
        self._disarm_silence()
        self._silence_task = asyncio.create_task(
            self._silence_watch(), name=f"silence:{self.call_id}"
        )

    def _disarm_silence(self) -> None:
        task = self._silence_task
        self._silence_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _silence_watch(self) -> None:
        wait_s = (
            SILENCE_OPENING_RETRY_S
            if not self._heard_caller and self._silence_prompts == 0
            else settings.silence_prompt_s
        )
        try:
            await asyncio.sleep(wait_s)
        except asyncio.CancelledError:
            return
        if (
            self.is_speaking
            or self._caller_speaking
            or self._turn_parts
            or self._turn_lock.locked()
            or self._closing
            or self._sealing
        ):
            return
        if not self._heard_caller:
            text, language = _opening_retry(self._silence_prompts == 0)
        else:
            text = phrases.silence_prompt(self.language, self._silence_prompts)
            language = self.language
        self._silence_prompts += 1
        await self.record("decision", {
            "stage": "silence_prompt",
            "attempt": self._silence_prompts,
            "silent_s": wait_s,
            "language": language,
        })
        self.agent.note_agent_line(text)
        await self.say(text, language=language)

    async def _send_media(self, frame: bytes) -> None:
        self._pad_tape("outbound")
        out = self._tape["outbound"]
        if len(out) < TAPE_CAP_BYTES:
            out.extend(frame)
        await self._send({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": base64.b64encode(frame).decode("ascii")},
        })

    async def _interrupt(self, heard: str) -> None:
        """The caller cut in: stop talking now and keep only what they heard."""
        self._generation += 1
        _drain(self._say_queue)
        _drain(self._audio_queue)

        spoken = self._spoken_so_far()
        self._speaking_since = None
        self.agent.note_interruption(spoken)

        await self._send({"event": "clear", "streamSid": self.stream_sid})
        await self.record("interruption", {"heard": heard, "agent_had_said": spoken})

    def _spoken_so_far(self) -> str:
        """Cut the current sentence where the audio actually stopped."""
        if self._speaking_since is None:
            # Cut off before playback began, so none of it reached the caller.
            return ""
        if not self._current_text or self._current_total <= 0:
            return self._current_text
        fraction = min(1.0, self._current_sent / self._current_total)
        words = self._current_text.split(" ")
        keep = max(1, int(len(words) * fraction))
        return " ".join(words[:keep])

    def _looks_like_echo(self, text: str) -> bool:
        """Skip a transcript that is just our own voice coming back on the line.

        STT almost never returns the TTS word-for-word, so a substring check
        misses most echo. Word overlap against what we just said catches the
        noisy remainder without needing a second audio pass.
        """
        heard = _words(text)
        if len(heard) < 2:
            return False
        said = _words(self._current_text) or _words(self._last_spoken)
        if not said:
            return False
        heard_line = " ".join(heard)
        said_line = " ".join(said)
        if heard_line in said_line or said_line in heard_line:
            return True
        overlap = len(set(heard) & set(said)) / len(set(heard))
        return overlap >= ECHO_OVERLAP

    def _worthy_barge_in(self, text: str, *, final: bool) -> bool:
        """Stop ourselves when the caller talks over us; never on a cough."""
        return len(_words(text)) >= MIN_BARGE_IN_WORDS

    @property
    def is_speaking(self) -> bool:
        """Playing, or already committed to playing.

        Synthesis takes a few hundred milliseconds, and a caller who starts
        talking inside that gap has to be able to stop us. Waiting for the first
        frame means the agent begins speaking over someone already mid-sentence
        and only notices afterwards.
        """
        return (
            self._speaking_since is not None
            or self._synthesizing > 0
            or not self._say_queue.empty()
            or not self._audio_queue.empty()
        )

    # ---- listening ----------------------------------------------------

    def _caller_is_talking(self) -> None:
        self._disarm_silence()
        self._silence_prompts = 0

    async def _on_speech_started(self) -> None:
        # Do not reset the silence clock here: room noise would cancel the
        # English re-greeting and leave the line idle until the harness cuts it.
        self._caller_speaking = True
        self._last_speech_started_at = time.monotonic()
        await self.record("caller_speaking", {})

    async def _on_partial(self, text: str) -> None:
        if not self._looks_like_echo(text):
            self._caller_is_talking()
        now = time.monotonic()
        self._caller_speaking = True
        self._waiting_since = None
        if now - self._last_partial_at > 0.4:
            self._last_partial_at = now
            await self.record("stt_partial", {"text": text})

        if not settings.barge_in or not self.is_speaking:
            return
        if self._looks_like_echo(text):
            return
        # The grace period guards against our own first frames; before any
        # audio is on the line there is nothing to guard.
        if self._speaking_since is not None:
            speaking_ms = (now - self._speaking_since) * 1000
            if speaking_ms < MIN_SPEAKING_MS_BEFORE_BARGE_IN:
                return
        if not self._worthy_barge_in(text, final=False):
            return
        await self._interrupt(text.strip())

    async def _on_final(self, text: str, language: Optional[str]) -> None:
        text = (text or "").strip()
        if not text:
            return
        gate = self._turn_gate is not None and self._turn_gate.enabled
        if not gate:
            self._caller_speaking = False
        self._waiting_since = None
        self._silence_prompts = 0
        if self._looks_like_echo(text):
            await self.record("stt_echo", {"text": text})
            return
        self._heard_caller = True
        if settings.barge_in and self.is_speaking and self._worthy_barge_in(text, final=True):
            await self._interrupt(text)
        decision = decide_language(
            text,
            _two_letter(language) if language else None,
            self.language,
            established=self._language_established,
        )
        apply_language = should_apply_language(
            self.language, self._language_established, decision
        )
        changed = apply_language and decision.code != self.language
        if apply_language and (changed or decision.confidence >= self.language_confidence):
            self.language = decision.code
            self.language_confidence = decision.confidence
            self.language_source = decision.source
        elif decision.code != self.language:
            await self.record("language_ignored", {
                "current": self.language,
                "candidate": decision.code,
                "confidence": decision.confidence,
                "source": decision.source,
                "text": text[:120],
            })
        if apply_language and decision.source in {"text_markers", "catalan_markers"}:
            self._language_established = True
        elif apply_language and decision.source == "deepgram_stream" and len(_words(text)) >= 5:
            self._language_established = True
        await self.record("stt_final", {
            "text": text,
            "language": self.language,
            "language_confidence": self.language_confidence,
            "language_source": self.language_source,
        })
        if changed:
            await self.record("language_detected", {
                "language": self.language,
                "confidence": self.language_confidence,
                "source": self.language_source,
            })
            self.agent.note_language(self.language)
            if self.transcriber is not None and hasattr(self.transcriber, "set_stream_language"):
                try:
                    await self.transcriber.set_stream_language(self.language)
                except Exception as exc:
                    await self.record("error", {
                        "where": "stt_language_switch",
                        "detail": f"{type(exc).__name__}: {exc}",
                    })
        if self._closed:
            return

        if not self._turn_parts or self._turn_parts[-1] != text:
            self._turn_parts.append(text)
        self._last_part_at = time.monotonic()
        self._disarm_turn_deadline()

        if gate and self._turn_gate is not None and not self._turn_gate.turn_over:
            # A noisy "Hello?" is not a turn. Wait until Smart Turn says they
            # have finished, then handle the whole request as one.
            self._arm_turn_deadline()
            return

        await self._commit_turn()

    async def _on_turn_event(self, event: str) -> None:
        if event == "start":
            self._caller_speaking = True
            self._disarm_silence()
            self._silence_prompts = 0
            return
        if event == "pause":
            await self._maybe_close_turn("vad_pause")
            return
        if event == "complete":
            self._caller_speaking = False
            await self._commit_turn()

    async def _maybe_close_turn(self, reason: str) -> None:
        if self._turn_gate is None or not self._turn_gate.enabled:
            return
        if self._turn_gate.turn_over:
            await self._commit_turn()
            return
        now = time.monotonic()
        if now - self._last_turn_decide_at < 0.3:
            return
        self._last_turn_decide_at = now
        complete = await self._turn_gate.decide()
        if self._turn_parts:
            await self.record("turn_decision", {
                "reason": reason,
                "complete": complete,
                "probability": self._turn_gate.last_probability,
                "parts": len(self._turn_parts),
            })
        if complete:
            self._caller_speaking = False
            await self._commit_turn()
            return
        self._arm_turn_deadline()

    def _arm_turn_deadline(self) -> None:
        """If the model stays incomplete, still answer — never sit on their words."""
        self._disarm_turn_deadline()
        wait_s = max(3.0, settings.smart_turn_stop_secs + 1.8)
        self._turn_deadline_task = asyncio.create_task(
            self._turn_deadline(wait_s), name=f"turn-deadline:{self.call_id}"
        )

    def _disarm_turn_deadline(self) -> None:
        task = self._turn_deadline_task
        self._turn_deadline_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _turn_deadline(self, wait_s: float) -> None:
        try:
            await asyncio.sleep(wait_s)
        except asyncio.CancelledError:
            return
        if not self._turn_parts or self._closed or self._closing:
            return
        await self.record("turn_decision", {
            "reason": "deadline",
            "complete": True,
            "parts": len(self._turn_parts),
        })
        self._caller_speaking = False
        await self._commit_turn()

    async def _commit_turn(self) -> None:
        """Give the LLM one joined caller turn, never a fragment."""
        if not self._turn_parts:
            return
        self._disarm_turn_deadline()
        text = " ".join(part for part in self._turn_parts if part).strip()
        self._turn_parts = []
        if self._turn_gate is not None:
            self._turn_gate.reset()
        if not text or self._closed:
            return
        if (
            text == self._last_committed
            or (self._last_committed and text in self._last_committed)
        ):
            return
        self._last_committed = text

        if self._turn_lock.locked():
            # The caller added something while we were still working: keep the
            # newest, because the last thing they asked for is the request.
            self._pending_turn = (
                f"{self._pending_turn} {text}".strip() if self._pending_turn else text
            )
            return

        self._caller_is_talking()
        async with self._turn_lock:
            pending: Optional[str] = text
            while pending:
                current, pending = pending, None
                await self.agent.handle(current)
                if self._pending_turn:
                    pending, self._pending_turn = self._pending_turn, None
        self._arm_silence()

    # ---- shared hooks --------------------------------------------------

    async def record(self, kind: str, payload: dict[str, Any]) -> None:
        await self.store.record(self.call_id, kind, payload)

    async def _client_event(self, kind: str, payload: dict[str, Any]) -> None:
        await self.record(kind, payload)

    async def note_response_latency(self, elapsed_ms: int) -> None:
        await self.record("turn_complete", {"response_ms": elapsed_ms})
        call = self.store.get(self.call_id)
        if call is not None:
            call.metrics["response_ms"].append(elapsed_ms)

    async def on_patient_identified(self, patient: dict[str, Any], context: dict[str, Any]) -> None:
        if patient not in self.seen_patients:
            self.seen_patients.append(patient)
        await self.record("patient_identified", {"patient": patient, "visit_count": context.get("visit_count")})
        await self.agent.brief_on_patient(patient, context)

    async def remember_matches(self, matches: list[dict[str, Any]]) -> None:
        for match in matches:
            if match not in self.seen_patients:
                self.seen_patients.append(match)

    async def submit(self, action: str, payload: dict[str, Any]) -> SubmitResult:
        """Send one action, and never send the same one twice."""
        if self._frozen or (self._sealing and action != "no_action"):
            result = SubmitResult(action, payload, 0, {"skipped": "record already closed"}, 0)
            await self.record("submit_skipped", result.as_dict())
            return result
        key = f"{action}:{sorted(payload.items())!r}"
        if key in self._submitted_keys:
            existing = next(
                (r for r in self.submissions if r.action == action and r.payload == payload), None
            )
            if existing is not None:
                return existing
        self._submitted_keys.add(key)

        if self.dry_run:
            result = SubmitResult(action, payload, 200, {"dry_run": True}, 0)
            await self.record("submit", {**result.as_dict(), "dry_run": True})
        else:
            result = await self.client.submit(action, payload)
        self.submissions.append(result)
        return result


# "[nombre del paciente]", "[full name]": a template slot, never a real word.
_PLACEHOLDER = re.compile(r"\[[^\]\d]{3,}\]")


# Transcribers disagree: Deepgram says "es", Scribe says "spa", and "spa"[:2]
# is not a language.
_ISO3 = {"cat": "ca", "spa": "es", "eng": "en", "glg": "gl", "eus": "eu", "baq": "eu"}


def _two_letter(code: str) -> str:
    code = code.lower().strip()
    return _ISO3.get(code, code.split("-")[0][:2])


def _drain(queue: asyncio.Queue) -> None:
    while not queue.empty():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break


def _words(text: str) -> list[str]:
    return [token for token in (text or "").lower().split() if token]


def _opening_retry(first: bool) -> tuple[str, str]:
    """Re-ask after the greeting if nobody has spoken yet."""
    if first:
        return phrases.OPENING_RETRY[0]
    return phrases.OPENING_RETRY[1]
