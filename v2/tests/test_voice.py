from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, EndFrame,
                                  LLMContextFrame, OutputAudioRawFrame, TextFrame, TTSAudioRawFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsHttpTTSService
from pipecat.workers.runner import WorkerRunner

from v2.audio import RecordedSocket, RunTape
from v2.clinic import Dispatcher, FixtureClinic
from v2.config import Config
from v2.models import CallState
from v2.store import RunStore
from v2.tests.test_workflow import NOW, booking, decision
from v2.voice import GraphProcessor, ProsperSerializer, voice_services
from v2.workflow import CallController


class CodecTests(unittest.IsolatedAsyncioTestCase):
    async def test_twilio_codec_and_stop_without_twilio_credentials(self):
        serializer = ProsperSerializer("stream", "call")
        self.assertFalse(serializer._params.auto_hang_up)
        await serializer.setup(SimpleNamespace(audio_in_sample_rate=8000))
        encoded = await serializer.serialize(TTSAudioRawFrame(audio=b"\0" * 320, sample_rate=8000, num_channels=1))
        message = json.loads(encoded)
        self.assertEqual(message["event"], "media")
        self.assertEqual(len(base64.b64decode(message["media"]["payload"])), 160)
        decoded = await serializer.deserialize(encoded)
        self.assertEqual(decoded.sample_rate, 8000)
        self.assertEqual(decoded.audio, b"\0" * 320)
        self.assertIsInstance(await serializer.deserialize('{"event":"stop"}'), EndFrame)

    async def test_audio_measurement_counts_only_successful_sends(self):
        with tempfile.TemporaryDirectory() as folder:
            tape = RunTape(Path(folder), "test-run")
            socket = SimpleNamespace(send_text=AsyncMock(side_effect=ConnectionError()))
            wrapped = RecordedSocket(socket, tape)
            message = json.dumps({"event": "media", "media": {"payload": base64.b64encode(b"\x80" * 160).decode()}})
            with self.assertRaises(ConnectionError):
                await wrapped.send_text(message)
            self.assertEqual(tape.signal_frames, 0)
            socket.send_text.side_effect = None
            await wrapped.send_text(message)
            self.assertEqual(tape.signal_frames, 1)
            self.assertEqual(tape.save()["audio_status"], "signal_sent")

    async def test_three_language_routes_are_explicit_without_connecting(self):
        config = Config(allow_paid=True, operator_token="test", gateway_key="test", deepgram_key="test",
                        cartesia_key="test", elevenlabs_key="test", voice_es="test-spanish")
        async with aiohttp.ClientSession() as session:
            stt, voices = voice_services(config, session)
            self.assertIsInstance(voices["en"], CartesiaTTSService)
            self.assertIsInstance(voices["es"], CartesiaTTSService)
            self.assertIsInstance(voices["ca"], ElevenLabsHttpTTSService)
            self.assertEqual(stt._settings.language, "multi")


class FixtureSpeaker(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame) and direction == FrameDirection.DOWNSTREAM:
            await self.push_frame(TTSAudioRawFrame(audio=b"\x01\x01" * 160, sample_rate=8000, num_channels=1))
        await self.push_frame(frame, direction)


class FixtureOutput(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.signal_frames = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
            self.signal_frames += 1
            await asyncio.sleep(0.02)
            await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await self.push_frame(frame, direction)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pipecat_pipeline_runs_graph_and_waits_for_audio(self):
        store = RunStore()
        self.addCleanup(store.close)
        decisions = iter([booking(), decision({"op": "confirm", "intent_id": "mine", "option": 1,
                                               "offer_revision": 1, "evidence": "Yes"}),
                          decision({"op": "finish"})])
        async def interpret(*_args):
            return next(decisions)
        state = CallState(call_id="pipeline", reference_time=NOW)
        controller = CallController(state, FixtureClinic(), store, Dispatcher(store, "simulation"),
                                    interpreter=SimpleNamespace(decide=interpret))
        output = FixtureOutput()
        graph = GraphProcessor(controller, sent_signal=lambda: output.signal_frames, grace_s=0.01)
        worker = PipelineWorker(Pipeline([graph, FixtureSpeaker(), output]),
                                params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000),
                                enable_rtvi=False, idle_timeout_secs=5)
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        ready = asyncio.Event()
        @worker.event_handler("on_pipeline_started")
        async def started(_worker, _frame):
            ready.set()
        async def end():
            await worker.queue_frame(EndFrame())
        graph.end_call = end
        task = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(ready.wait(), 3)
            for text in ("Please book for Lina Demo, born 1990-01-01", "Yes, option one", "Thank you"):
                context = LLMContext()
                context.add_message({"role": "user", "content": text})
                before = output.signal_frames
                await worker.queue_frame(LLMContextFrame(context))
                for _ in range(200):
                    if output.signal_frames > before and graph.active is None:
                        break
                    await asyncio.sleep(0.01)
                self.assertGreater(output.signal_frames, before)
            await asyncio.wait_for(task, 3)
            self.assertTrue(state.all_resolved)
            self.assertEqual(state.intents["mine"].receipts[0]["source"], "simulation")
            self.assertTrue(any(e["kind"] == "completion_close" for e in store.report(state.run_id)["events"]))
        finally:
            await runner.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
