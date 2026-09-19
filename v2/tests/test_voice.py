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
from pipecat.services.tts_service import TTSService
from pipecat.workers.runner import WorkerRunner

from v2.audio import RecordedSocket, RunTape
from v2.clinic import Dispatcher, FixtureClinic
from v2.config import Config
from v2.models import CallState
from v2.store import RunStore
from v2.tests.test_workflow import NOW, booking, decision
from v2.voice import GraphProcessor, PacedAudioOutput, ProsperSerializer, TrackedTTS, voice_services
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
            self.assertEqual(stt._init_sample_rate, 16000)
            self.assertTrue(all(voice._init_sample_rate == 24000 for voice in voices.values()))
            self.assertEqual(voices["en"]._settings.language, "en")
            self.assertEqual(voices["es"]._settings.language, "es")
            self.assertEqual(voices["ca"]._settings.language, "ca")
            self.assertEqual(voices["ca"]._settings.model, "eleven_v3")

    async def test_catalan_never_silently_uses_a_non_catalan_model(self):
        config = SimpleNamespace(missing_voice=lambda: [], voice_en="en", voice_es="es", voice_ca="ca",
                                 elevenlabs_model="eleven_flash_v2_5")
        async with aiohttp.ClientSession() as session:
            with self.assertRaises(ValueError):
                voice_services(config, session)

    async def test_missing_stock_voice_is_rejected_before_service_setup(self):
        config = SimpleNamespace(missing_voice=lambda: [], voice_en="en", voice_es="es", voice_ca="")
        async with aiohttp.ClientSession() as session:
            with self.assertRaises(PermissionError):
                voice_services(config, session)


class FixtureSpeaker(TrackedTTS, TTSService):
    def __init__(self):
        super().__init__(sample_rate=24000, push_start_frame=True, push_stop_frames=True)

    async def run_tts(self, text, context_id):
        yield TTSAudioRawFrame(audio=b"\x10\x10" * 480, sample_rate=24000, num_channels=1, context_id=context_id)


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
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tape = RunTape(Path(temporary.name), state.run_id)
        wrapped = RecordedSocket(SimpleNamespace(send_text=AsyncMock()), tape)
        graph = GraphProcessor(controller, sent_signal=lambda: tape.signal_frames, grace_s=0.01)
        output = PacedAudioOutput(wrapped, "stream", graph, tail_s=0)
        graph.output = output
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
                before = tape.signal_frames
                await worker.queue_frame(LLMContextFrame(context))
                for _ in range(200):
                    if tape.signal_frames > before and graph.active is None:
                        break
                    await asyncio.sleep(0.01)
                self.assertGreater(tape.signal_frames, before)
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
