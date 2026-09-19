from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable

import numpy as np
import soxr
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, CancelFrame, EndFrame, ErrorFrame, Frame,
    InterruptionFrame, LLMContextFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame,
    ManuallySwitchServiceFrame, MetricsFrame, STTUpdateSettingsFrame, TextFrame, TTSAudioRawFrame,
    UserStartedSpeakingFrame, UserStoppedSpeakingFrame, VADUserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.service_switcher import ServiceSwitcher
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsHttpTTSService
from pipecat.services.elevenlabs.tts_base import ELEVENLABS_MODEL_LANGUAGES
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_start.min_words_user_turn_start_strategy import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner
from starlette.websockets import WebSocketState

from v2.codecs import pcm16_to_ulaw
from v2.audio import MediaProtocolError, RecordedSocket, RunTape, parse_message
from v2.config import Config
from v2.models import Reply
from v2.workflow import CallController, TEXT


class ProsperSerializer(TwilioFrameSerializer):
    def __init__(self, stream_sid: str, call_sid: str):
        if not stream_sid or not call_sid:
            raise ValueError("stream and original call identifiers are required")
        super().__init__(stream_sid=stream_sid, call_sid=call_sid,
                         params=TwilioFrameSerializer.InputParams(auto_hang_up=False, resampler_clear_after_secs=None))

    async def deserialize(self, data: str | bytes) -> Frame | None:
        value, _ = parse_message(data, self._stream_sid)
        if value["event"] == "stop":
            return EndFrame()
        if value["event"] == "clear":
            raise MediaProtocolError("unexpected inbound clear")
        return await super().deserialize(data)


def response_tag(reply: Reply) -> dict:
    return {"response_id": reply.response_id, "epoch": reply.epoch}


class TrackedTTS:
    def __init__(self, *args, **kwargs):
        self._voice_tag = {}
        self._voice_contexts: dict[str, dict] = {}
        self._voice_turns: dict[str, dict] = {}
        super().__init__(*args, **kwargs)

    async def process_frame(self, frame, direction):
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._voice_tag = dict(frame.metadata)
                key = self._voice_tag.get("response_id")
                if key:
                    if len(self._voice_turns) >= 64:
                        self._voice_turns.pop(next(iter(self._voice_turns)))
                    self._voice_turns[key] = {"contexts": set(), "closed": set(), "failed": False, "ended": False}
            elif isinstance(frame, LLMFullResponseEndFrame):
                turn = self._voice_turns.get(frame.metadata.get("response_id"))
                if turn is not None:
                    turn["ended"] = True
            elif isinstance(frame, InterruptionFrame):
                for turn in self._voice_turns.values():
                    turn["failed"] = True
        await super().process_frame(frame, direction)

    async def on_turn_context_created(self, context_id):
        self._voice_contexts[context_id] = dict(self._voice_tag)
        if len(self._voice_contexts) > 128:
            self._voice_contexts.pop(next(iter(self._voice_contexts)))
        turn = self._voice_turns.get(self._voice_tag.get("response_id"))
        if turn is not None:
            turn["contexts"].add(context_id)
        await super().on_turn_context_created(context_id)

    async def append_to_audio_context(self, context_id, frame):
        if isinstance(frame, ErrorFrame):
            tag = self._voice_contexts.get(context_id, {})
            turn = self._voice_turns.get(tag.get("response_id"))
            if turn is not None:
                turn["failed"] = True
            frame.error = "voice synthesis failed"
        await super().append_to_audio_context(context_id, frame)

    async def remove_audio_context(self, context_id):
        tag = self._voice_contexts.get(context_id, {})
        turn = self._voice_turns.get(tag.get("response_id"))
        if turn is not None:
            turn["closed"].add(context_id)
        await super().remove_audio_context(context_id)

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        context_id = getattr(frame, "context_id", None)
        if context_id:
            frame.metadata.update(self._voice_contexts.get(context_id, {}))
        if isinstance(frame, LLMFullResponseEndFrame):
            turn = self._voice_turns.get(frame.metadata.get("response_id"))
            frame.metadata["synthesis_complete"] = bool(
                turn and turn["ended"] and not turn["failed"] and turn["contexts"]
                and turn["contexts"] == turn["closed"]
            )
        if isinstance(frame, ErrorFrame):
            for turn in self._voice_turns.values():
                turn["failed"] = True
            frame.error = "voice provider failure"
        await super().push_frame(frame, direction)


class StockCartesiaTTS(TrackedTTS, CartesiaTTSService):
    pass


class CatalanElevenLabsTTS(TrackedTTS, ElevenLabsHttpTTSService):
    pass


class GraphProcessor(FrameProcessor):
    def __init__(self, controller: CallController, *, voices: dict | None = None, stt=None,
                 sent_signal: Callable[[], int] = lambda: 0, grace_s: float = 15,
                 paid: bool = False, response_timeout_s: float = 90, turn_timeout_s: float = 30,
                 uncommitted_speech_timeout_s: float = 5):
        super().__init__()
        self.controller, self.voices, self.stt = controller, voices or {}, stt
        self.sent_signal, self.grace_s, self.paid = sent_signal, max(0, grace_s), paid
        self.response_timeout_s, self.turn_timeout_s = response_timeout_s, turn_timeout_s
        self.uncommitted_speech_timeout_s = uncommitted_speech_timeout_s
        self.vad_guard_task: asyncio.Task | None = None
        self.completion_epoch: int | None = None
        self.last_vad_at = float("-inf")
        self.jobs: set[asyncio.Task] = set()
        self.turns: set[asyncio.Task] = set()
        self.active: Reply | None = None
        self.close_task: asyncio.Task | None = None
        self.response_task: asyncio.Task | None = None
        self.end_call = None
        self.abort_call = None
        self.output: PacedAudioOutput | None = None
        self.language = None if stt is not None else controller.state.language
        self.ending = False
        self.bot_speaking = False
        self.turn_started = False
        self.user_speaking = False
        self.seen_metrics: set[int] = set()
        self._cleaned = False
        self._cleanup_job: asyncio.Task | None = None

    def event(self, kind: str, **data):
        self.controller.store.event(self.controller.state.run_id, kind, data)

    def _cancel_timer(self, name):
        task = getattr(self, name)
        if task and task is not asyncio.current_task():
            task.cancel()
        setattr(self, name, None)

    def invalidate(self, source: str):
        self.active = None
        self._cancel_timer("close_task")
        self._cancel_timer("response_task")
        self._cancel_timer("vad_guard_task")
        self.completion_epoch = None
        self.controller.interrupt(source=source)
        if self.output:
            self.output.invalidate()

    def accepts(self, tag: dict) -> bool:
        return bool(not self.ending and self.active and tag.get("response_id") == self.active.response_id
                    and tag.get("epoch") == self.active.epoch == self.controller.epoch)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame) and direction == FrameDirection.DOWNSTREAM:
            self.user_speaking = self.turn_started = True
            self.invalidate("caller" if self.bot_speaking else "caller_input")
        elif isinstance(frame, InterruptionFrame) and direction == FrameDirection.DOWNSTREAM:
            if not self.turn_started:
                self.invalidate("caller" if self.bot_speaking else "caller_input")
            self.bot_speaking = False
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.user_speaking = False
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self.last_vad_at = time.monotonic()
            self._cancel_timer("close_task")
            self._cancel_timer("vad_guard_task")
            if self.completion_epoch is not None:
                self.vad_guard_task = asyncio.create_task(self._resume_after_uncommitted_speech(self.completion_epoch))
        elif isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            messages = frame.context.get_messages()
            text = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
            if isinstance(text, str) and text.strip() and not self.ending:
                if not self.turn_started:
                    self.invalidate("caller_input")
                self.turn_started = self.user_speaking = False
                if len(text) > 2000 or len(self.jobs) >= 4:
                    await self.fail("turn_queue_limit")
                    return
                job = asyncio.create_task(self.respond(text, self.controller.epoch))
                self.jobs.add(job)
                job.add_done_callback(self.jobs.discard)
            return
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            self._cancel_timer("close_task")
        elif isinstance(frame, MetricsFrame) and frame.id not in self.seen_metrics:
            self.seen_metrics.add(frame.id)
            if len(self.seen_metrics) > 512:
                self.seen_metrics = {frame.id}
            for item in frame.data:
                self.event("pipeline_metric", metric=type(item).__name__, **item.model_dump(mode="json"))
        elif isinstance(frame, ErrorFrame):
            frame.error = "voice pipeline failure"
            frame.metadata["voice_handled"] = True
            await self.fail("provider_error")
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self.ending = True
            self.invalidate("shutdown")
        await self.push_frame(frame, direction)

    async def respond(self, text: str, epoch: int):
        turn = asyncio.create_task(self.controller.turn(text, expected_epoch=epoch))
        self.turns.add(turn)
        turn.add_done_callback(self.turns.discard)
        try:
            done, _ = await asyncio.wait({turn}, timeout=self.turn_timeout_s)
            if not done:
                if epoch == self.controller.epoch:
                    await self.fail("turn_timeout")
                await asyncio.shield(turn)
                return
            reply = turn.result()
            if reply and not self.ending:
                await self.speak(reply)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if epoch == self.controller.epoch:
                await self.fail(type(exc).__name__)

    async def fail(self, error_type: str):
        self.event("pipeline_error", type=error_type)
        self.invalidate("pipeline_error")
        await self.push_frame(InterruptionFrame())

    async def speak(self, reply: Reply):
        if self.ending or reply.epoch != self.controller.epoch:
            return
        if not reply.text.strip() or len(reply.text) > 1600 or reply.language not in {"en", "es", "ca"}:
            raise ValueError("invalid bounded voice response")
        if self.paid:
            self.controller.store.reserve(self.controller.state.run_id, "tts", 1_000_000)
        if reply.language != self.language and self.stt is not None:
            stt_language = Language.CA if reply.language == "ca" else "multi"
            await self.push_frame(STTUpdateSettingsFrame(delta=DeepgramSTTService.Settings(language=stt_language),
                                                        service=self.stt), FrameDirection.UPSTREAM)
        self.language = reply.language
        if self.voices:
            await self.push_frame(ManuallySwitchServiceFrame(service=self.voices[reply.language]))
        self.active = reply
        self._cancel_timer("response_task")
        self.response_task = asyncio.create_task(self._response_deadline(reply))
        for frame in (LLMFullResponseStartFrame(), TextFrame(text=reply.text), LLMFullResponseEndFrame()):
            frame.metadata.update(response_tag(reply))
            await self.push_frame(frame)

    async def _response_deadline(self, reply):
        await asyncio.sleep(self.response_timeout_s)
        if self.accepts(response_tag(reply)):
            await self.fail("response_timeout")

    async def audio_complete(self, tag: dict, signal: bool):
        if not self.accepts(tag):
            return
        reply = self.active
        if not signal:
            await self.fail("silent_response")
            return
        self.active = None
        self._cancel_timer("response_task")
        self.controller.presented(reply)
        self.event("voice_response_sent", response_id=reply.response_id, epoch=reply.epoch,
                   observation="complete_synthesis_paced_socket_send_not_playback_acknowledged")
        if reply.completion and self.end_call and self.controller.state.completion_requested:
            self.completion_epoch = reply.epoch
            if time.monotonic() - self.last_vad_at < self.uncommitted_speech_timeout_s:
                self.vad_guard_task = asyncio.create_task(self._resume_after_uncommitted_speech(reply.epoch))
            else:
                self.close_task = asyncio.create_task(self.close_when_quiet(reply.epoch))

    async def _resume_after_uncommitted_speech(self, epoch: int):
        await asyncio.sleep(self.uncommitted_speech_timeout_s)
        if (not self.ending and epoch == self.controller.epoch and self.completion_epoch == epoch
                and self.controller.state.completion_requested and not self.user_speaking):
            self.close_task = asyncio.create_task(self.close_when_quiet(epoch))

    async def close_when_quiet(self, epoch: int):
        await asyncio.sleep(self.grace_s)
        if (not self.jobs and not self.turns and not self.user_speaking and not self.active and not self.ending
                and epoch == self.controller.epoch and self.controller.state.completion_requested):
            self.event("completion_close", epoch=epoch)
            await self.end_call()

    async def cleanup(self):
        if self._cleanup_job is None:
            self._cleanup_job = asyncio.create_task(self._cleanup())
        try:
            await asyncio.shield(self._cleanup_job)
        except asyncio.CancelledError:
            await self._cleanup_job
            raise

    async def _cleanup(self):
        self._cleaned = self.ending = True
        timers = [t for t in (self.close_task, self.response_task, self.vad_guard_task) if t]
        self.invalidate("shutdown")
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        pending = tuple(self.jobs | self.turns)
        if pending:
            _, remaining = await asyncio.wait(pending, timeout=5)
            for job in remaining:
                job.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await super().cleanup()


class PacedAudioOutput(FrameProcessor):
    def __init__(self, socket: RecordedSocket, stream_sid: str, graph: GraphProcessor, *, tail_s: float = 0.1):
        super().__init__()
        self.socket, self.stream_sid, self.graph = socket, stream_sid, graph
        self.tail_s = max(0, tail_s)
        self._queued_seconds = 0.0
        self.invalidate()

    def invalidate(self):
        self._tag = {}
        self._buffer = bytearray()
        self._resampler = None
        self._rate = None
        self._signal = False
        self._speaking = False
        self._next_send = 0.0
        self._queued_seconds = 0.0

    async def queue_frame(self, frame, direction=FrameDirection.DOWNSTREAM, callback=None):
        if isinstance(frame, TTSAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            if not self.graph.accepts(frame.metadata):
                return
            duration = len(frame.audio) / (2 * max(1, frame.sample_rate))
            if duration > 120 or self._queued_seconds + duration > 120:
                await self.graph.fail("audio_queue_limit")
                return
            self._queued_seconds += duration
        await super().queue_frame(frame, direction, callback)

    async def _send(self, value, tag=None):
        return await self.socket.send_text_if_current(
            json.dumps({"streamSid": self.stream_sid, **value}),
            lambda: tag is None or self.graph.accepts(tag),
        )

    async def _drain(self, tag: dict, *, final=False):
        if final and self._buffer:
            self._buffer.extend(b"\0" * ((320 - len(self._buffer) % 320) % 320))
        while len(self._buffer) >= 320 and self.graph.accepts(tag):
            packet = bytes(self._buffer[:320])
            del self._buffer[:320]
            await asyncio.sleep(max(0, self._next_send - time.monotonic()))
            if not self.graph.accepts(tag):
                return
            payload = pcm16_to_ulaw(packet)
            sent = await self._send({"event": "media", "media": {"payload": base64.b64encode(payload).decode("ascii")}}, tag)
            if not sent or not self.graph.accepts(tag):
                return
            self._next_send = time.monotonic() + 0.02
            self._signal = self._signal or bool(payload.translate(None, b"\xff\x7f"))
            if not self._speaking:
                self._speaking = True
                await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.UPSTREAM:
            await self.push_frame(frame, direction)
            return
        try:
            if isinstance(frame, (InterruptionFrame, CancelFrame, EndFrame)):
                self.invalidate()
                if isinstance(frame, InterruptionFrame):
                    await self._send({"event": "clear"})
                    await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
            elif isinstance(frame, LLMFullResponseStartFrame):
                if not self.graph.accepts(frame.metadata):
                    return
                self.invalidate()
                self._tag = dict(frame.metadata)
            elif isinstance(frame, TTSAudioRawFrame):
                self._queued_seconds = max(0, self._queued_seconds - len(frame.audio) / (2 * max(1, frame.sample_rate)))
                if not self.graph.accepts(frame.metadata) or frame.metadata.get("response_id") != self._tag.get("response_id"):
                    return
                if frame.num_channels != 1 or frame.sample_rate not in {8000, 16000, 22050, 24000, 44100, 48000} or len(frame.audio) % 2:
                    raise ValueError("invalid provider PCM format")
                if self._rate is None:
                    self._rate = frame.sample_rate
                    if self._rate != 8000:
                        self._resampler = soxr.ResampleStream(self._rate, 8000, 1, dtype="int16", quality="HQ")
                if self._rate != frame.sample_rate:
                    raise ValueError("provider changed sample rate within response")
                pcm = frame.audio
                if self._resampler is not None:
                    pcm = self._resampler.resample_chunk(np.frombuffer(pcm, dtype="<i2")).astype("<i2").tobytes()
                self._buffer.extend(pcm)
                await self._drain(dict(frame.metadata))
                return
            elif isinstance(frame, LLMFullResponseEndFrame):
                tag = dict(frame.metadata)
                if not self.graph.accepts(tag) or tag.get("response_id") != self._tag.get("response_id"):
                    return
                if not tag.get("synthesis_complete"):
                    await self.graph.fail("incomplete_synthesis")
                    return
                if self._resampler is not None:
                    self._buffer.extend(self._resampler.resample_chunk(np.empty(0, dtype="int16"), last=True).astype("<i2").tobytes())
                await self._drain(tag, final=True)
                await asyncio.sleep(max(0, self._next_send - time.monotonic()) + self.tail_s)
                if not self.graph.accepts(tag):
                    return
                sent = await self._send({"event": "mark", "mark": {"name": "v2-" + tag["response_id"]}}, tag)
                if not sent or not self.graph.accepts(tag):
                    return
                await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
                await self.graph.audio_complete(tag, self._signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.invalidate()
            await self.graph.fail(type(exc).__name__)
            if self.graph.abort_call:
                await self.graph.abort_call()
            return
        await self.push_frame(frame, direction)


def voice_services(config: Config, http_session):
    missing = config.missing_voice()
    if missing:
        raise PermissionError("voice providers are not ready: " + ", ".join(missing))
    if not all((config.voice_en, config.voice_es, config.voice_ca)):
        raise PermissionError("all three stock voice identifiers are required")
    ca_model = getattr(config, "elevenlabs_model", "eleven_v3")
    if "ca" not in ELEVENLABS_MODEL_LANGUAGES.get(ca_model, ()):
        raise ValueError("Catalan requires an ElevenLabs model with explicit ca support")
    stt = DeepgramSTTService(api_key=config.deepgram_key, sample_rate=16000, mip_opt_out=True,
                             settings=DeepgramSTTService.Settings(model="nova-3", language="multi",
                                                                  numerals=True, smart_format=True))
    voices = {
        language: StockCartesiaTTS(api_key=config.cartesia_key, sample_rate=24000,
                                  settings=CartesiaTTSService.Settings(
                                      model=getattr(config, "cartesia_model", "sonic-3.6-2026-08-27"), voice=voice,
                                      language=Language.EN if language == "en" else Language.ES))
        for language, voice in (("es", config.voice_es), ("en", config.voice_en))
    }
    voices["ca"] = CatalanElevenLabsTTS(
        api_key=config.elevenlabs_key, aiohttp_session=http_session, sample_rate=24000,
        settings=ElevenLabsHttpTTSService.Settings(model=ca_model, voice=config.voice_ca, language=Language.CA),
    )
    return stt, voices


def user_aggregators(config, *, vad_analyzer):
    return LLMContextAggregatorPair(LLMContext(), user_params=LLMUserAggregatorParams(
        vad_analyzer=vad_analyzer,
        user_turn_strategies=UserTurnStrategies(
            start=[MinWordsUserTurnStartStrategy(min_words=1)],
            stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=getattr(config, "user_speech_timeout_s", 0.6))],
        ),
    ))


async def run_voice(socket, stream_sid: str, controller: CallController, config: Config):
    import aiohttp

    tape = RunTape(config.data_dir, controller.state.run_id)
    wrapped = RecordedSocket(socket, tape, stream_sid=stream_sid,
                             send_timeout_s=getattr(config, "voice_send_timeout_s", 5))
    runner = None
    graph = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60, connect=10, sock_read=15)) as http_session:
            stt, voices = voice_services(config, http_session)
            controller.store.reserve(controller.state.run_id, "stt_call", 1_000_000)
            transport = FastAPIWebsocketTransport(wrapped, FastAPIWebsocketParams(
                audio_in_enabled=True, audio_out_enabled=False, audio_in_sample_rate=16000,
                add_wav_header=False, session_timeout=config.call_limit_s, allowed_origins=[],
                serializer=ProsperSerializer(stream_sid, controller.state.call_id),
            ))
            user, assistant = user_aggregators(config, vad_analyzer=SileroVADAnalyzer(sample_rate=16000))
            graph = GraphProcessor(controller, voices=voices, stt=stt, sent_signal=lambda: tape.signal_frames,
                                   grace_s=config.completion_grace_s, paid=True,
                                   turn_timeout_s=getattr(config, "voice_turn_timeout_s", 30),
                                   response_timeout_s=getattr(config, "voice_response_timeout_s", 90))
            output = PacedAudioOutput(wrapped, stream_sid, graph,
                                      tail_s=getattr(config, "voice_playout_tail_s", 0.1))
            graph.output = output
            pipeline = Pipeline([transport.input(), stt, user, graph, ServiceSwitcher(list(voices.values())), output, assistant])
            worker = PipelineWorker(pipeline, params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000,
                                                                   enable_metrics=True, enable_usage_metrics=True),
                                    enable_rtvi=False, idle_timeout_secs=None)
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(worker)
            async def end_call():
                await worker.queue_frame(EndFrame())
            graph.end_call = end_call
            graph.abort_call = runner.cancel

            @worker.event_handler("on_pipeline_started")
            async def started(_worker, _frame):
                if controller.state.turn or controller.pending_reply is not None:
                    return
                greeting = Reply(text=TEXT[controller.state.language]["hello"], language=controller.state.language,
                                 epoch=controller.epoch)
                controller.pending_reply = greeting
                controller.store.event(controller.state.run_id, "response_planned", {**greeting.model_dump(), "elapsed_ms": 0})
                await graph.speak(greeting)

            @worker.event_handler("on_pipeline_error")
            async def pipeline_error(_worker, frame):
                frame.error = "voice provider failure"
                if not frame.metadata.get("voice_handled"):
                    await graph.fail("provider_error")
                if frame.fatal or (frame.processor and not frame.processor.is_usable):
                    await runner.cancel()

            @worker.event_handler("on_setup_timeout")
            async def setup_timeout(_worker):
                graph.event("pipeline_error", type="setup_timeout")
                graph.ending = True
                graph.invalidate("setup_timeout")
                await runner.cancel()

            @worker.event_handler("on_pipeline_timeout")
            async def pipeline_timeout(_worker, frame):
                graph.event("pipeline_error", type="pipeline_timeout", frame=type(frame).__name__)
                graph.ending = True
                graph.invalidate("pipeline_timeout")
                await runner.cancel()

            @transport.event_handler("on_client_disconnected")
            async def disconnected(_transport, _client):
                graph.ending = True
                graph.invalidate("disconnect")
                await runner.cancel()

            @transport.event_handler("on_session_timeout")
            async def timed_out(_transport, _client):
                graph.event("deadline", seconds=config.call_limit_s)
                graph.ending = True
                graph.invalidate("deadline")
                await runner.cancel()

            try:
                await asyncio.wait_for(runner.run(), timeout=config.call_limit_s + 10)
            finally:
                await runner.cancel()
                await graph.cleanup()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        controller.store.event(controller.state.run_id, "pipeline_error", {"type": type(exc).__name__})
        raise
    finally:
        if graph:
            await graph.cleanup()
        if getattr(socket, "application_state", None) != WebSocketState.DISCONNECTED:
            try:
                await asyncio.wait_for(socket.close(), timeout=0.5)
            except Exception as exc:
                controller.store.event(controller.state.run_id, "socket_close_error", {"type": type(exc).__name__})
        if tape.protocol_errors:
            controller.store.event(controller.state.run_id, "protocol_error", {"count": tape.protocol_errors})
        try:
            controller.store.event(controller.state.run_id, "audio_output", tape.save(finalized=True))
        except OSError as exc:
            controller.store.event(controller.state.run_id, "audio_recording_error", {"type": type(exc).__name__})
        controller.store.save(controller.state)
