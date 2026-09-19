from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pipecat.frames.frames import (BotStartedSpeakingFrame, EndFrame, ErrorFrame, InterruptionFrame,
                                  LLMContextFrame, TTSAudioRawFrame, TranscriptionFrame,
                                  VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.tts_service import TTSService
from pipecat.workers.runner import WorkerRunner

from v2.audio import RecordedSocket, RunTape
from v2.models import Reply
from v2.tests.test_voice_realtime import controller, tagged
from v2.tests.test_voice_socket import eventually
from v2.voice import GraphProcessor, PacedAudioOutput, TrackedTTS, user_aggregators


class ControlledSpeaker(TrackedTTS, TTSService):
    def __init__(self, *, fail=False):
        super().__init__(sample_rate=8000, push_start_frame=True, push_stop_frames=True)
        self.cancelled = asyncio.Event()
        self.fail = fail

    async def run_tts(self, text, context_id):
        old = "Old" in text
        try:
            yield TTSAudioRawFrame(audio=(b"\x00\x10" if old else b"\x00\xf0") * 800,
                                   sample_rate=8000, num_channels=1, context_id=context_id)
            if self.fail:
                yield ErrorFrame(error="sensitive body must not be forwarded")
            elif old:
                await asyncio.sleep(5)
                yield TTSAudioRawFrame(audio=b"\x00\x10" * 8000, sample_rate=8000, num_channels=1, context_id=context_id)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class ContextCapture(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.messages = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            self.messages.append(frame.context.get_messages()[-1]["content"])
        await self.push_frame(frame, direction)


class MachineryTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, processors):
        worker = PipelineWorker(Pipeline(processors), enable_rtvi=False, idle_timeout_secs=None,
                                params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=8000))
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        ready = asyncio.Event()
        @worker.event_handler("on_pipeline_started")
        async def started(_worker, _frame):
            ready.set()
        task = asyncio.create_task(runner.run())
        async def finish():
            await runner.cancel()
            await asyncio.wait_for(task, 8)
        self.addAsyncCleanup(finish)
        await asyncio.wait_for(ready.wait(), 3)
        return worker

    async def test_one_word_final_survives_real_aggregator_while_bot_is_speaking(self):
        user, assistant = user_aggregators(SimpleNamespace(user_speech_timeout_s=0.02), vad_analyzer=None)
        capture = ContextCapture()
        worker = await self.start([user, capture, assistant])
        await worker.queue_frame(BotStartedSpeakingFrame())
        await worker.queue_frame(VADUserStartedSpeakingFrame())
        await worker.queue_frame(TranscriptionFrame(text="Sí", user_id="fixture", timestamp="0", finalized=True))
        await worker.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))
        await eventually(lambda: bool(capture.messages))
        self.assertEqual(capture.messages, ["Sí"])

    async def test_real_tts_queue_is_interrupted_and_late_audio_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            ctl = controller()
            graph = GraphProcessor(ctl)
            sent = []
            async def send(text):
                sent.append(json.loads(text))
            socket = RecordedSocket(SimpleNamespace(send_text=send), RunTape(Path(root), "interrupt"))
            output = PacedAudioOutput(socket, "stream", graph, tail_s=0)
            graph.output = output
            speaker = ControlledSpeaker()
            worker = await self.start([graph, speaker, output])
            old = Reply(text="Old response.", language="en", epoch=0)
            await graph.speak(old)
            await eventually(lambda: any(m["event"] == "media" for m in sent))
            await worker.queue_frame(InterruptionFrame())
            await eventually(lambda: ctl.epoch == 1)
            await eventually(speaker.cancelled.is_set)
            await asyncio.sleep(0.06)
            after = len([m for m in sent if m["event"] == "media"])
            await output.queue_frame(tagged(TTSAudioRawFrame(b"\x00\x10" * 160, 8000, 1), old))
            await asyncio.sleep(0.03)
            self.assertEqual(after, len([m for m in sent if m["event"] == "media"]))
            ctl.presented.assert_not_called()
            new = Reply(text="New response.", language="en", epoch=1)
            await graph.speak(new)
            await eventually(lambda: ctl.presented.called)
            ctl.presented.assert_called_once_with(new)
            tail = [base64.b64decode(m["media"]["payload"]) for m in sent if m["event"] == "media"][after:]
            self.assertTrue(tail)
            self.assertTrue(all(packet == b"\x2f" * 160 for packet in tail))
            self.assertTrue(any(m["event"] == "clear" for m in sent))

    async def test_partial_provider_error_does_not_present_or_close(self):
        with tempfile.TemporaryDirectory() as root:
            ctl = controller()
            graph = GraphProcessor(ctl, grace_s=0.01)
            graph.end_call = AsyncMock()
            socket = RecordedSocket(SimpleNamespace(send_text=AsyncMock()), RunTape(Path(root), "error"))
            output = PacedAudioOutput(socket, "stream", graph, tail_s=0)
            graph.output = output
            await self.start([graph, ControlledSpeaker(fail=True), output])
            await graph.speak(Reply(text="New response.", language="en", epoch=0, completion=True))
            await eventually(lambda: ctl.epoch > 0)
            await asyncio.sleep(0.05)
            ctl.presented.assert_not_called()
            graph.end_call.assert_not_called()
            self.assertNotIn("sensitive body", str(ctl.store.event.call_args_list))
