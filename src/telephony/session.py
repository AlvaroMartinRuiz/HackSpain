"""One call, start to finish.

Everything a call owns lives on one of these: its own transcriber, its own
synthesiser, its own conversation and its own record. Nothing is shared
between sockets, which is what lets twenty of them run at once.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, Awaitable, Callable, Optional

from src.agent.brain import Agent
from src.agent.llm import LLMClient
from src.config import settings
from src.domain.catalog import Catalog
from src.domain.engine import SchedulingEngine
from src.obs.store import CallStore
from src.platform_api.client import PlatformClient, SubmitResult
from src.voice import audio
from src.voice.stt import build_transcriber
from src.voice.tts import Synthesizer, build_synthesizer

Sender = Callable[[dict[str, Any]], Awaitable[None]]

# A single word from a noisy line is not an interruption.
MIN_BARGE_IN_CHARS = 7
MIN_SPEAKING_MS_BEFORE_BARGE_IN = 350
PLAYBACK_LEAD_S = 0.20


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
    ) -> None:
        self.call_id = call_id
        self.stream_sid = stream_sid
        self.from_number = from_number
        self.catalog = catalog
        self.store = store
        self._send = send
        self.text_mode = text_mode
        self.dry_run = dry_run

        self.client = PlatformClient(on_event=self._client_event)
        self.engine = SchedulingEngine(self.client, catalog)
        self.agent = Agent(self, llm)
        self.synthesizer: Optional[Synthesizer] = None if text_mode else build_synthesizer()
        self.transcriber = None if text_mode else build_transcriber(
            on_partial=self._on_partial,
            on_final=self._on_final,
            on_speech_started=self._on_speech_started,
        )

        self.seen_patients: list[dict[str, Any]] = []
        self.submissions: list[SubmitResult] = []
        self._submitted_keys: set[str] = set()

        self._say_queue: asyncio.Queue = asyncio.Queue()
        self._audio_queue: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._generation = 0
        self._speaking_since: Optional[float] = None
        self._current_text = ""
        self._current_sent = 0
        self._current_total = 0
        self._turn_lock = asyncio.Lock()
        self._pending_turn: Optional[str] = None
        self._closed = False
        self._timed_out = False
        self._allow_timeout_submit = False
        self.language = "es"
        self._started_at = time.monotonic()
        self._last_partial_at = 0.0

    # ---- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        if not self.text_mode:
            assert self.transcriber is not None
            await self.transcriber.start()
            self._tasks = [
                asyncio.create_task(self._speaker_loop(), name=f"speaker:{self.call_id}"),
                asyncio.create_task(self._player_loop(), name=f"player:{self.call_id}"),
                asyncio.create_task(self._deadline_loop(), name=f"deadline:{self.call_id}"),
            ]
        await self.record("decision", {"stage": "greeting", "why": "call connected"})
        await self.agent.greet()

    async def on_media(self, payload: str) -> None:
        if self.transcriber is None:
            return
        try:
            chunk = base64.b64decode(payload)
        except (ValueError, TypeError):
            return
        await self.transcriber.push(chunk)

    async def on_stop(self) -> None:
        if self.transcriber is not None:
            await self.transcriber.finish()

    async def feed_text(self, text: str) -> None:
        """A caller turn typed instead of spoken, for rehearsal and evaluation."""
        await self._on_final(text, None)

    async def finalize(self, status: str = "finished") -> None:
        """Close the call, and never leave its record empty."""
        if self._closed:
            return
        self._closed = True

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        await self._guarantee_submission()

        if self.transcriber is not None:
            try:
                await self.transcriber.finish()
            except Exception:
                pass
        if self.synthesizer is not None:
            await self.synthesizer.aclose()
        await self.client.aclose()

        self.store.close_call(self.call_id, status)
        await self.store.announce({"type": "call_ended", "call_id": self.call_id})

    async def _guarantee_submission(self) -> None:
        """Silence is always wrong, so something is always reported."""
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
        """A call that cannot be done in three minutes has failed anyway."""
        try:
            await asyncio.sleep(settings.call_hard_limit_s)
        except asyncio.CancelledError:
            raise
        self._timed_out = True
        await self.record("error", {"where": "deadline", "detail": "hard call limit reached"})
        self._allow_timeout_submit = True
        try:
            await self._guarantee_submission()
        finally:
            self._allow_timeout_submit = False

    # ---- speaking -----------------------------------------------------

    async def say(self, text: str, first: bool = False) -> None:
        text = (text or "").strip()
        if not text:
            return
        # Logged when decided rather than when finished playing, so the console
        # shows the turn as the caller starts hearing it.
        await self.record("agent_said", {"text": text, "greeting": first})
        if not self.text_mode:
            await self._say_queue.put((self._generation, text, self.language))

    async def _speaker_loop(self) -> None:
        while True:
            generation, text, language = await self._say_queue.get()
            if generation != self._generation:
                continue
            await self._synthesize(generation, text, language)

    async def _synthesize(self, generation: int, text: str, language: str) -> None:
        started = time.perf_counter()
        first_byte_ms: Optional[int] = None
        total = 0
        try:
            async for chunk in self.synthesizer.stream(text, language):
                if generation != self._generation:
                    return
                if first_byte_ms is None:
                    first_byte_ms = int((time.perf_counter() - started) * 1000)
                total += len(chunk)
                await self._audio_queue.put((generation, text, chunk))
        except Exception as exc:
            await self.record("error", {"where": "tts", "detail": str(exc)})
            return

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
            for frame in audio.frames(chunk):
                if generation != self._generation:
                    break
                now = time.monotonic()
                if playhead > now + PLAYBACK_LEAD_S:
                    await asyncio.sleep(playhead - now - PLAYBACK_LEAD_S)
                await self._send_media(frame)
                self._current_sent += len(frame)
                playhead += audio.FRAME_MS / 1000

            if self._audio_queue.empty() and self._say_queue.empty():
                await self._finish_speaking(text)
                playhead = max(playhead, time.monotonic())

    async def _finish_speaking(self, text: str) -> None:
        if self._speaking_since is None:
            return
        self._speaking_since = None
        await self.record("agent_turn_end", {"text": text})

    async def _send_media(self, frame: bytes) -> None:
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
        if not self._current_text or self._current_total <= 0:
            return self._current_text
        fraction = min(1.0, self._current_sent / self._current_total)
        words = self._current_text.split(" ")
        keep = max(1, int(len(words) * fraction))
        return " ".join(words[:keep])

    @property
    def is_speaking(self) -> bool:
        return self._speaking_since is not None

    # ---- listening ----------------------------------------------------

    async def _on_speech_started(self) -> None:
        await self.record("caller_speaking", {})

    async def _on_partial(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last_partial_at > 0.4:
            self._last_partial_at = now
            await self.record("stt_partial", {"text": text})

        if not settings.barge_in or not self.is_speaking:
            return
        speaking_ms = (now - (self._speaking_since or now)) * 1000
        if speaking_ms < MIN_SPEAKING_MS_BEFORE_BARGE_IN:
            return
        if len(text.strip()) < MIN_BARGE_IN_CHARS:
            return
        await self._interrupt(text.strip())

    async def _on_final(self, text: str, language: Optional[str]) -> None:
        await self.record("stt_final", {"text": text, "language": language})
        if language:
            self.language = language.lower()[:2]
        if self._closed or self._timed_out:
            return

        if self._turn_lock.locked():
            # The caller added something while we were still working: keep the
            # newest, because the last thing they asked for is the request.
            self._pending_turn = text
            return

        async with self._turn_lock:
            pending: Optional[str] = text
            while pending:
                current, pending = pending, None
                await self.agent.handle(current)
                if self._pending_turn:
                    pending, self._pending_turn = self._pending_turn, None

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
        if self._timed_out and not self._allow_timeout_submit:
            result = SubmitResult(
                action, payload, 410, {"error": "call hard limit already reached"}, 0
            )
            await self.record("submit", {**result.as_dict(), "blocked_locally": True})
            self.submissions.append(result)
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


def _drain(queue: asyncio.Queue) -> None:
    while not queue.empty():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break
