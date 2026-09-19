from __future__ import annotations

import asyncio
import json
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pipecat.frames.frames import (BotStoppedSpeakingFrame, LLMFullResponseEndFrame,
                                  LLMFullResponseStartFrame, TTSAudioRawFrame,
                                  UserStartedSpeakingFrame, VADUserStartedSpeakingFrame)
from pipecat.processors.frame_processor import FrameDirection

from v2.audio import RecordedSocket, RunTape
from v2.models import Reply
from v2.voice import GraphProcessor, PacedAudioOutput


DOWN = FrameDirection.DOWNSTREAM


def tagged(frame, reply, *, complete=False):
    frame.metadata.update(response_id=reply.response_id, epoch=reply.epoch)
    if complete:
        frame.metadata["synthesis_complete"] = True
    return frame


def controller():
    state = SimpleNamespace(language="en", run_id="unit", completion_requested=False)
    result = SimpleNamespace(state=state, epoch=0, store=SimpleNamespace(event=Mock(), reserve=Mock()),
                             presented=Mock(), turn=AsyncMock())
    def interrupt(source="caller"):
        result.epoch += 1
        state.completion_requested = False
    result.interrupt = Mock(side_effect=interrupt)
    return result


class PresentationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = controller()
        self.graph = GraphProcessor(self.controller, grace_s=0.03, response_timeout_s=2)
        self.graph.push_frame = AsyncMock()
        self.tape = RunTape(Path(self.temp.name), "unit")
        self.sent = []
        async def send(text):
            self.sent.append(json.loads(text))
        self.socket = RecordedSocket(SimpleNamespace(send_text=send), self.tape)
        self.output = PacedAudioOutput(self.socket, "stream", self.graph)
        self.output.push_frame = AsyncMock()
        self.graph.output = self.output
        self.addAsyncCleanup(self.graph.cleanup)

    async def start_reply(self, completion=False):
        reply = Reply(text="First sentence. Second sentence.", language="en", epoch=self.controller.epoch,
                      completion=completion)
        await self.graph.speak(reply)
        await self.output.process_frame(tagged(LLMFullResponseStartFrame(), reply), DOWN)
        return reply

    async def audio(self, reply, samples=160):
        frame = TTSAudioRawFrame(audio=b"\x10\x10" * samples, sample_rate=8000, num_channels=1)
        await self.output.process_frame(tagged(frame, reply), DOWN)

    async def test_audio_is_paced_at_real_time_on_a_coarse_clock(self):
        stamps = []
        async def send(text):
            if json.loads(text).get("event") == "media":
                stamps.append(time.perf_counter())
        self.socket.socket = SimpleNamespace(send_text=send)
        reply = await self.start_reply()
        await self.audio(reply, samples=8000)
        self.assertEqual(len(stamps), 50)
        elapsed = stamps[-1] - stamps[0]
        # Never meaningfully ahead of real time (barge-in), and not starving the caller either.
        self.assertTrue(all(t - stamps[0] >= k * 0.02 - 0.02 for k, t in enumerate(stamps)))
        self.assertLess(elapsed, 49 * 0.02 * 1.15)

    async def test_first_synthesized_sentence_does_not_present_full_offer(self):
        reply = await self.start_reply()
        await self.audio(reply)
        await self.graph.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        self.controller.presented.assert_not_called()
        self.assertIs(self.graph.active, reply)
        await self.audio(reply, 97)
        await self.output.process_frame(tagged(LLMFullResponseEndFrame(), reply, complete=True), DOWN)
        self.controller.presented.assert_called_once_with(reply)
        self.assertEqual([m["event"] for m in self.sent], ["media", "media", "mark"])
        self.assertTrue(all(len(m["media"]["payload"]) == 216 for m in self.sent if m["event"] == "media"))

    async def test_provider_timeout_end_is_not_full_synthesis(self):
        reply = await self.start_reply()
        await self.audio(reply)
        await self.output.process_frame(tagged(LLMFullResponseEndFrame(), reply), DOWN)
        self.controller.presented.assert_not_called()
        self.assertFalse(self.controller.state.completion_requested)

    async def test_interruption_during_pacing_drops_tail_without_remote_clear(self):
        reply = await self.start_reply()
        task = asyncio.create_task(self.audio(reply, 8000))
        while not self.sent:
            await asyncio.sleep(0.001)
        await self.graph.process_frame(UserStartedSpeakingFrame(), DOWN)
        count = len([m for m in self.sent if m["event"] == "media"])
        await task
        await self.audio(reply)
        await self.output.process_frame(tagged(LLMFullResponseEndFrame(), reply, complete=True), DOWN)
        self.assertEqual(len([m for m in self.sent if m["event"] == "media"]), count)
        self.controller.presented.assert_not_called()

    async def test_stale_generation_cannot_contaminate_new_response(self):
        old = await self.start_reply()
        await self.graph.process_frame(UserStartedSpeakingFrame(), DOWN)
        new = await self.start_reply()
        await self.audio(old)
        self.assertFalse(any(m["event"] == "media" for m in self.sent))
        await self.audio(new)
        await self.output.process_frame(tagged(LLMFullResponseEndFrame(), old, complete=True), DOWN)
        self.controller.presented.assert_not_called()
        await self.output.process_frame(tagged(LLMFullResponseEndFrame(), new, complete=True), DOWN)
        self.controller.presented.assert_called_once_with(new)

    async def test_completion_grace_starts_after_last_paced_sample_and_new_vad_revokes_it(self):
        reply = await self.start_reply(completion=True)
        self.controller.state.completion_requested = True
        self.graph.end_call = AsyncMock()
        await self.audio(reply, 640)
        self.graph.end_call.assert_not_called()
        await self.output.process_frame(tagged(LLMFullResponseEndFrame(), reply, complete=True), DOWN)
        await self.graph.process_frame(VADUserStartedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.05)
        self.graph.end_call.assert_not_called()

    async def test_silence_never_counts_as_presented(self):
        reply = await self.start_reply()
        await self.output.process_frame(tagged(TTSAudioRawFrame(b"\0" * 320, 8000, 1), reply), DOWN)
        await self.output.process_frame(tagged(LLMFullResponseEndFrame(), reply, complete=True), DOWN)
        self.controller.presented.assert_not_called()

    async def test_send_failure_never_presents_and_revokes_completion(self):
        reply = await self.start_reply(completion=True)
        self.controller.state.completion_requested = True
        self.socket.socket.send_text = AsyncMock(side_effect=ConnectionError("private provider body"))
        await self.audio(reply)
        await self.output.process_frame(tagged(LLMFullResponseEndFrame(), reply, complete=True), DOWN)
        self.controller.presented.assert_not_called()
        self.assertFalse(self.controller.state.completion_requested)
        self.assertNotIn("private provider body", str(self.controller.store.event.call_args_list))

    async def test_uncommitted_vad_has_a_bounded_guard_not_permanent_silence(self):
        self.graph.uncommitted_speech_timeout_s = 0.02
        reply = await self.start_reply(completion=True)
        self.controller.state.completion_requested = True
        self.graph.end_call = AsyncMock()
        await self.audio(reply)
        await self.output.process_frame(tagged(LLMFullResponseEndFrame(), reply, complete=True), DOWN)
        await self.graph.process_frame(VADUserStartedSpeakingFrame(), DOWN)
        await asyncio.sleep(0.015)
        self.graph.end_call.assert_not_called()
        await asyncio.sleep(0.08)
        self.graph.end_call.assert_awaited_once()

    async def test_resampler_flushes_last_samples_before_presentation(self):
        reply = await self.start_reply()
        frame = TTSAudioRawFrame(audio=b"\x10\x10" * 483, sample_rate=24000, num_channels=1)
        await self.output.process_frame(tagged(frame, reply), DOWN)
        await self.output.process_frame(tagged(LLMFullResponseEndFrame(), reply, complete=True), DOWN)
        self.assertEqual(len([m for m in self.sent if m["event"] == "media"]), 2)
        self.controller.presented.assert_called_once_with(reply)

    async def test_cleanup_drains_an_already_running_turn_without_cancelling_it(self):
        started, release = asyncio.Event(), asyncio.Event()
        cancelled = False
        async def turn(*_args, **_kwargs):
            nonlocal cancelled
            started.set()
            try:
                await release.wait()
                return None
            except asyncio.CancelledError:
                cancelled = True
                raise
        self.controller.turn.side_effect = turn
        job = asyncio.create_task(self.graph.respond("Confirm", 0))
        self.graph.jobs.add(job)
        job.add_done_callback(self.graph.jobs.discard)
        await started.wait()
        cleanup = asyncio.create_task(self.graph.cleanup())
        await asyncio.sleep(0.01)
        self.assertFalse(cleanup.done())
        release.set()
        await cleanup
        self.assertFalse(cancelled)
        self.assertTrue(job.done())

    async def test_calls_do_not_share_generations(self):
        other = GraphProcessor(controller(), grace_s=0.01)
        other.push_frame = AsyncMock()
        self.addAsyncCleanup(other.cleanup)
        reply = await self.start_reply()
        other.invalidate("caller")
        await self.audio(reply)
        await self.output.process_frame(tagged(LLMFullResponseEndFrame(), reply, complete=True), DOWN)
        self.controller.presented.assert_called_once_with(reply)
