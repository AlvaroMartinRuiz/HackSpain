from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, EndFrame, ErrorFrame, Frame,
    InterruptionFrame, LLMContextFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame,
    ManuallySwitchServiceFrame, MetricsFrame, STTUpdateSettingsFrame, TextFrame, UserStartedSpeakingFrame,
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
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_start.min_words_user_turn_start_strategy import MinWordsUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from v2.audio import RecordedSocket, RunTape
from v2.config import Config
from v2.models import Reply
from v2.workflow import CallController, TEXT


class ProsperSerializer(TwilioFrameSerializer):
    def __init__(self, stream_sid: str, call_sid: str):
        super().__init__(stream_sid=stream_sid, call_sid=call_sid,
                         params=TwilioFrameSerializer.InputParams(auto_hang_up=False))

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if json.loads(data).get("event") == "stop":
            return EndFrame()
        return await super().deserialize(data)


class GraphProcessor(FrameProcessor):
    def __init__(self, controller: CallController, *, voices: dict | None = None, stt=None,
                 sent_signal: Callable[[], int] = lambda: 0, grace_s: float = 15,
                 paid: bool = False):
        super().__init__()
        self.controller, self.voices, self.stt = controller, voices or {}, stt
        self.sent_signal, self.grace_s, self.paid = sent_signal, grace_s, paid
        self.jobs: set[asyncio.Task] = set()
        self.active: Reply | None = None
        self.audio_baseline = 0
        self.close_task: asyncio.Task | None = None
        self.end_call = None
        self.language = controller.state.language
        self.ending = False
        self.bot_speaking = False
        self.seen_metrics: set[int] = set()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (InterruptionFrame, UserStartedSpeakingFrame)):
            source = "caller" if isinstance(frame, InterruptionFrame) and self.bot_speaking else "caller_input"
            self.controller.interrupt(source=source)
            if isinstance(frame, InterruptionFrame):
                self.bot_speaking = False
            self.active = None
            if self.close_task:
                self.close_task.cancel()
        elif isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            messages = frame.context.get_messages()
            text = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
            if isinstance(text, str) and text.strip() and not self.ending:
                job = asyncio.create_task(self.respond(text, self.controller.epoch))
                self.jobs.add(job)
                job.add_done_callback(self.jobs.discard)
            return
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
            reply, self.active = self.active, None
            if reply is not None and self.sent_signal() > self.audio_baseline and reply.epoch == self.controller.epoch:
                self.controller.presented(reply)
                if reply.completion and self.end_call:
                    self.close_task = asyncio.create_task(self.close_when_quiet(reply.epoch))
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            if self.close_task:
                self.close_task.cancel()
        elif isinstance(frame, MetricsFrame) and frame.id not in self.seen_metrics:
            self.seen_metrics.add(frame.id)
            if len(self.seen_metrics) > 512:
                self.seen_metrics = {frame.id}
            for item in frame.data:
                self.controller.store.event(self.controller.state.run_id, "pipeline_metric",
                                            {"metric": type(item).__name__, **item.model_dump(mode="json")})
        elif isinstance(frame, ErrorFrame):
            self.active = None
            self.controller.state.completion_requested = False
            self.controller.store.event(self.controller.state.run_id, "pipeline_error", {"type": type(frame).__name__})
        elif isinstance(frame, EndFrame):
            self.ending = True
            if self.close_task:
                self.close_task.cancel()
            if self.jobs:
                await asyncio.wait(tuple(self.jobs), timeout=5)
        await self.push_frame(frame, direction)

    async def respond(self, text: str, epoch: int):
        try:
            reply = await self.controller.turn(text, expected_epoch=epoch)
            if reply and not self.ending:
                await self.speak(reply)
        except Exception as exc:
            self.controller.store.event(self.controller.state.run_id, "pipeline_error", {"type": type(exc).__name__})
            if self.end_call:
                await self.end_call()

    async def speak(self, reply: Reply):
        if reply.epoch != self.controller.epoch:
            return
        if len(reply.text) > 1600:
            raise ValueError("response exceeds the bounded speech allowance")
        if self.paid:
            self.controller.store.reserve(self.controller.state.run_id, "tts", 1_000_000)
        if reply.language != self.language and self.stt is not None:
            stt_language = Language.CA if reply.language == "ca" else "multi"
            await self.push_frame(STTUpdateSettingsFrame(settings=DeepgramSTTService.Settings(language=stt_language),
                                                        service=self.stt), FrameDirection.UPSTREAM)
        self.language = reply.language
        if self.voices:
            await self.push_frame(ManuallySwitchServiceFrame(service=self.voices[reply.language]))
        self.active = reply
        self.audio_baseline = self.sent_signal()
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(TextFrame(text=reply.text))
        await self.push_frame(LLMFullResponseEndFrame())

    async def close_when_quiet(self, epoch: int):
        await asyncio.sleep(self.grace_s)
        if not self.jobs and epoch == self.controller.epoch and self.controller.state.completion_requested:
            self.controller.store.event(self.controller.state.run_id, "completion_close", {"epoch": epoch})
            await self.end_call()

    async def cleanup(self):
        if self.close_task:
            self.close_task.cancel()
        self.controller.interrupt(source="shutdown")
        if self.jobs:
            pending = tuple(self.jobs)
            done, remaining = await asyncio.wait(pending, timeout=5)
            for job in remaining:
                job.cancel()
            if remaining:
                await asyncio.gather(*remaining, return_exceptions=True)
        await super().cleanup()


def voice_services(config: Config, http_session):
    missing = config.missing_voice()
    if missing:
        raise PermissionError("voice providers are not ready: " + ", ".join(missing))
    stt = DeepgramSTTService(api_key=config.deepgram_key, sample_rate=16000, mip_opt_out=True,
                             settings=DeepgramSTTService.Settings(model="nova-3", language="multi",
                                                                  numerals=True, smart_format=True))
    voices = {
        language: CartesiaTTSService(api_key=config.cartesia_key, sample_rate=8000,
                                    settings=CartesiaTTSService.Settings(model="sonic-3.6-2026-08-27", voice=voice,
                                                                         language=Language.EN if language == "en" else Language.ES))
        for language, voice in (("es", config.voice_es), ("en", config.voice_en))
    }
    voices["ca"] = ElevenLabsHttpTTSService(
        api_key=config.elevenlabs_key, aiohttp_session=http_session, sample_rate=8000,
        settings=ElevenLabsHttpTTSService.Settings(model="eleven_v3_conversational", voice=config.voice_ca,
                                                  language=Language.CA),
    )
    return stt, voices


async def run_voice(socket, stream_sid: str, controller: CallController, config: Config):
    import aiohttp

    controller.store.reserve(controller.state.run_id, "stt_call", 1_000_000)
    tape = RunTape(config.data_dir, controller.state.run_id)
    wrapped = RecordedSocket(socket, tape)
    transport = FastAPIWebsocketTransport(wrapped, FastAPIWebsocketParams(
        audio_in_enabled=True, audio_out_enabled=True, audio_in_sample_rate=16000, audio_out_sample_rate=8000,
        audio_out_10ms_chunks=2, add_wav_header=False, session_timeout=config.call_limit_s,
        serializer=ProsperSerializer(stream_sid, controller.state.call_id),
    ))
    async with aiohttp.ClientSession() as http_session:
        stt, voices = voice_services(config, http_session)
        context = LLMContext()
        user, assistant = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(sample_rate=16000),
            user_turn_strategies=UserTurnStrategies(start=[MinWordsUserTurnStartStrategy(min_words=2),
                                                          MinWordsUserTurnStartStrategy(min_words=1, use_interim=False)],
                                                   stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)]),
        ))
        graph = GraphProcessor(controller, voices=voices, stt=stt, sent_signal=lambda: tape.signal_frames,
                               grace_s=config.completion_grace_s, paid=True)
        pipeline = Pipeline([transport.input(), stt, user, graph, ServiceSwitcher(list(voices.values())),
                             transport.output(), assistant])
        worker = PipelineWorker(pipeline, params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=8000,
                                                               enable_metrics=True, enable_usage_metrics=True),
                                enable_rtvi=False, idle_timeout_secs=40)
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        async def end_call():
            await worker.queue_frame(EndFrame())
        graph.end_call = end_call

        @worker.event_handler("on_pipeline_started")
        async def started(_worker, _frame):
            greeting = Reply(text=TEXT[controller.state.language]["hello"], language=controller.state.language,
                             epoch=controller.epoch)
            await graph.speak(greeting)

        @transport.event_handler("on_client_disconnected")
        async def disconnected(_transport, _client):
            await runner.cancel()

        @transport.event_handler("on_session_timeout")
        async def timed_out(_transport, _client):
            controller.store.event(controller.state.run_id, "deadline", {"seconds": config.call_limit_s})
            await runner.cancel()

        try:
            await asyncio.wait_for(runner.run(), timeout=config.call_limit_s + 10)
        finally:
            await runner.cancel()
            controller.store.event(controller.state.run_id, "audio_output", tape.save())
            controller.store.save(controller.state)
