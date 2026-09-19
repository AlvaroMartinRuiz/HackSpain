from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = ":memory:"
os.environ["TTS_WARM_CACHE"] = "false"

from src.agent.llm import Completion, ToolCall
from src.config import settings
from src.domain.catalog import Catalog
from src.obs.store import CallStore
from src.platform_api.client import SubmitResult
from src.telephony import session as module
from src.telephony.session import CallSession


class CallFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = CallStore(":memory:")
        self.addCleanup(self.store._db.close)
        self.store.open_call("flow", None)
        self.send = AsyncMock()
        self.close = AsyncMock()
        self.session = CallSession("flow", "stream", None, self.send, Catalog.load(), self.store, None,
                                   text_mode=True, dry_run=True, close=self.close)
        self.session.text_mode = False
        self.session._heard_caller = True
        self.addAsyncCleanup(self.cleanup_session)
        self.constants = patch.multiple(module, COMPLETED_CLOSE_S=0.04, SPEECH_HOLD_S=0.01,
                                        SILENCE_CLOSE_S=0.04)
        self.constants.start()
        self.addCleanup(self.constants.stop)
        self.configuration = patch.object(module, "settings", replace(settings, silence_prompt_s=0.02))
        self.configuration.start()
        self.addCleanup(self.configuration.stop)

    async def cleanup_session(self):
        self.session._disarm_silence()
        for task in self.session._tasks:
            task.cancel()
        await asyncio.gather(*self.session._tasks, return_exceptions=True)
        await self.session.client.aclose()

    async def accepted(self):
        await self.session.submit("book", {"call_id": "flow", "patient_id": "test-patient"})

    async def complete(self):
        return await self.session.agent.tools.dispatch("finish_call", {"all_requests_resolved": True})

    async def test_submission_alone_does_not_close_a_multi_intent_call(self):
        await self.accepted()
        with patch.object(self.session, "finalize", new_callable=AsyncMock) as finalize:
            await self.session._silence_watch()
        finalize.assert_not_called()
        self.close.assert_not_called()

    async def test_explicit_completion_closes_after_quiet_without_another_prompt(self):
        await self.accepted()
        self.assertTrue((await self.complete())["closing_when_quiet"])
        self.session._speaking_since = time.monotonic()
        await self.session._send_media(b"\x80" * 160)
        with patch.object(self.session, "_arm_silence"):
            await self.session._finish_speaking("Your appointment is confirmed. Goodbye.")
        with patch.object(self.session, "finalize", new_callable=AsyncMock) as finalize:
            await self.session._silence_watch()
        finalize.assert_awaited_once_with("completed")
        self.close.assert_awaited_once()
        self.assertEqual(self.session._silence_prompts, 0)

    async def test_completion_requires_resolved_requests_and_successful_submission(self):
        self.assertFalse((await self.complete()).get("closing_when_quiet", False))
        self.session.submissions.append(SubmitResult("book", {}, 422, {}, 1))
        self.assertFalse((await self.complete()).get("closing_when_quiet", False))
        await self.accepted()
        result = await self.session.agent.tools.dispatch("finish_call", {"all_requests_resolved": False})
        self.assertFalse(result.get("closing_when_quiet", False))

    async def test_new_caller_request_revokes_completion(self):
        await self.accepted()
        await self.complete()
        with patch.object(self.session, "finalize", new_callable=AsyncMock) as finalize:
            self.session._arm_silence()
            await self.session._on_partial("And I also need an appointment for my child")
            self.assertFalse(self.session._completion_requested)
            await asyncio.sleep(0.015)
        finalize.assert_not_called()
        self.close.assert_not_called()

    async def test_failed_second_intent_blocks_completion_despite_first_success(self):
        await self.accepted()
        result = await self.session.agent.tools.dispatch("book_slot", {"caller_confirmed": False})
        self.assertFalse(result["booked"])
        self.assertFalse((await self.complete())["closing_when_quiet"])

    async def test_more_tool_work_revokes_completion(self):
        await self.accepted()
        await self.complete()
        await self.session.agent.tools.dispatch("clinic_facts", {"topic": "sites"})
        self.assertFalse(self.session._completion_requested)

    async def test_completion_never_closes_during_playback_or_tool_execution(self):
        await self.accepted()
        await self.complete()
        self.session._speaking_since = time.monotonic()
        with patch.object(self.session, "finalize", new_callable=AsyncMock) as finalize:
            await self.session._silence_watch()
            self.session._speaking_since = None
            async with self.session._turn_lock:
                await self.session._silence_watch()
        finalize.assert_not_called()

    async def test_completion_without_spoken_confirmation_does_not_close(self):
        await self.accepted()
        await self.complete()
        with patch.object(self.session, "finalize", new_callable=AsyncMock) as finalize:
            await self.session._silence_watch()
        finalize.assert_not_called()
        self.close.assert_not_called()

    async def test_pending_caller_input_blocks_completion(self):
        await self.accepted()
        await self.session._final_turns.put(("Also my son", "en"))
        self.assertFalse((await self.complete())["closing_when_quiet"])

    async def test_exhausted_silence_prompts_eventually_close(self):
        self.session._silence_prompts = settings.silence_prompt_max
        with patch.object(self.session, "finalize", new_callable=AsyncMock) as finalize:
            self.session._arm_silence()
            self.assertIsNotNone(self.session._silence_task)
            await self.session._silence_task
        finalize.assert_awaited_once_with("idle_timeout")
        self.close.assert_awaited_once()

    async def test_stale_vad_event_does_not_disable_silence_handling_forever(self):
        await self.session._on_speech_started()
        await self.session._silence_watch()
        self.assertEqual(self.session._silence_prompts, 1)

    async def test_echo_partial_does_not_mark_the_caller_as_speaking(self):
        self.session._last_spoken = "Your appointment is on Thursday"
        await self.session._on_partial("Your appointment is on Thursday")
        self.assertFalse(self.session._caller_speaking)

    async def test_no_old_frame_escapes_after_clear_during_pacing_sleep(self):
        real_sleep = asyncio.sleep
        sleeping, resume = asyncio.Event(), asyncio.Event()
        messages = []
        async def send(message):
            messages.append(message["event"])
        async def pause(_delay):
            sleeping.set()
            await resume.wait()
        self.session._send = send
        await self.session._audio_queue.put((0, "a long answer", b"\x80" * 8000))
        with patch.object(module.asyncio, "sleep", pause):
            player = asyncio.create_task(self.session._player_loop())
            self.session._tasks.append(player)
            await asyncio.wait_for(sleeping.wait(), 1)
            await self.session._interrupt("No, I meant something else")
            resume.set()
            await real_sleep(0.01)
            player.cancel()
            await asyncio.gather(player, return_exceptions=True)
        clear = messages.index("clear")
        self.assertNotIn("media", messages[clear + 1:])

    async def test_interruption_cancels_the_llm_stream_not_just_queued_audio(self):
        started, closed = asyncio.Event(), asyncio.Event()
        class BlockingLLM:
            async def stream(_self, *_args):
                try:
                    yield "text", "Let me explain the available options. "
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    closed.set()
        self.session.agent.llm = BlockingLLM()
        turn = asyncio.create_task(self.session.agent.handle("I need a doctor"))
        self.session._tasks.append(turn)
        await asyncio.wait_for(started.wait(), 1)
        await self.session._interrupt("Wait, I need to cancel instead")
        await asyncio.wait_for(turn, 0.3)
        self.assertTrue(closed.is_set())
        self.assertTrue(self.session._say_queue.empty())
        self.assertFalse(self.store.get("flow").errors)

    async def test_shutdown_cancellation_is_not_swallowed_after_interruption(self):
        started = asyncio.Event()
        class BlockingLLM:
            async def stream(_self, *_args):
                started.set()
                await asyncio.Event().wait()
                yield "done", Completion(text="never")
        self.session.agent.llm = BlockingLLM()
        turn = asyncio.create_task(self.session.agent.handle("I need a doctor"))
        self.session._tasks.append(turn)
        await asyncio.wait_for(started.wait(), 1)
        await self.session._interrupt("Changed my mind")
        turn.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await turn

    async def test_interruption_does_not_cancel_an_inflight_write_or_start_another(self):
        entered, release = asyncio.Event(), asyncio.Event()
        completion = Completion(tool_calls=[ToolCall("one", "book_slot", "{}"),
                                             ToolCall("two", "cancel_appointment", "{}")])
        class ToolLLM:
            async def stream(_self, *_args):
                yield "done", completion
        async def dispatch(name, args):
            entered.set()
            await release.wait()
            await self.accepted()
            return {"booked": True}
        self.session.agent.llm = ToolLLM()
        with patch.object(self.session.agent.tools, "dispatch", side_effect=dispatch) as tool:
            turn = asyncio.create_task(self.session.agent.handle("Yes, book that one"))
            self.session._tasks.append(turn)
            await asyncio.wait_for(entered.wait(), 1)
            await self.session._interrupt("Wait, another question")
            release.set()
            await asyncio.wait_for(turn, 0.3)
            self.assertEqual(tool.await_count, 1)
        self.assertEqual(len(self.session.submissions), 1)
        self.assertTrue(self.session.submissions[0].accepted)

    async def test_two_intent_voice_call_keeps_both_records_then_closes_cleanly(self):
        rounds = iter([
            Completion(tool_calls=[ToolCall("book-first", "book_slot", '{"patient_id":"first"}')]),
            Completion(text="The first appointment is confirmed. What does your child need?"),
            Completion(tool_calls=[ToolCall("book-second", "book_slot", '{"patient_id":"second"}')]),
            Completion(tool_calls=[ToolCall("finish", "finish_call", '{"all_requests_resolved":true}')]),
            Completion(text="Both appointments are confirmed. Goodbye."),
        ])
        class FixtureLLM:
            async def stream(_self, *_args):
                result = next(rounds)
                if result.text:
                    yield "text", result.text
                yield "done", result
        class FixtureTTS:
            async def stream(_self, *_args):
                yield b"\x80" * 800
            async def aclose(_self):
                pass
        async def book(args):
            result = await self.session.submit("book", {"call_id": "flow", "patient_id": args["patient_id"]})
            return {"booked": result.accepted}
        closed = asyncio.Event()
        self.session._close_wire = AsyncMock(side_effect=closed.set)
        self.session.agent.llm = FixtureLLM()
        self.session.synthesizer = FixtureTTS()
        self.session.transcriber = SimpleNamespace(start=AsyncMock(), finish=AsyncMock())
        with patch.object(self.session.agent, "greet", new_callable=AsyncMock), patch.object(
            self.session.agent.tools, "_tool_book_slot", side_effect=book
        ), patch.object(self.session, "_save_tape"):
            await self.session.start()
            await self.session._queue_final("Book mine first, then my child's appointment", "en")
            await asyncio.wait_for(self.session._settled(), 1)
            self.assertEqual(len(self.session.submissions), 1)
            self.assertFalse(self.session._completion_requested)
            self.assertFalse(closed.is_set())
            await self.session._queue_final("My child needs paediatrics, please book that too", "en")
            await asyncio.wait_for(closed.wait(), 3)
        self.assertEqual([s.payload["patient_id"] for s in self.session.submissions], ["first", "second"])
        self.assertEqual(self.store.get("flow").status, "completed")
        self.assertEqual(self.store.get("flow").summary()["audio_status"], "signal")
        self.assertFalse(self.store.get("flow").errors)

    async def test_stt_intake_is_nonblocking_and_preserves_followup_fragments(self):
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def handle(text):
            seen.append(text)
            if len(seen) == 1:
                entered.set()
                await release.wait()
        self.session.agent.handle = handle
        worker = asyncio.create_task(self.session._turn_worker())
        self.session._turn_worker_task = worker
        self.session._tasks.append(worker)
        await asyncio.wait_for(self.session._queue_final("I want an appointment", "en"), 0.1)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(self.session._queue_final("Also for my daughter", "en"), 0.1)
        await asyncio.wait_for(self.session._queue_final("She needs paediatrics", "en"), 0.1)
        waiter = asyncio.create_task(self.session._settled())
        self.session._tasks.append(waiter)
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        release.set()
        await asyncio.wait_for(waiter, 0.3)
        self.assertIn("Also for my daughter", " ".join(seen[1:]))
        self.assertIn("She needs paediatrics", " ".join(seen[1:]))


if __name__ == "__main__":
    unittest.main()
