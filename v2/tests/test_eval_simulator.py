from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import ValidationError

from v2.config import Config
from v2.models import CallState, TurnDecision
from v2.providers import VercelInterpreter
from v2.simulator import CallerTurn, ModelCaller, run_duel
from v2.store import RunStore


class NaturalSimulationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = RunStore(str(Path(self.folder.name) / "runs.db"))
        self.addCleanup(self.store.close)
        self.config = Config(allow_paid=True, gateway_key="mock-secret", mode="simulation", data_dir=Path(self.folder.name))
        self.state = CallState(call_id="mock-caller", reference_time=datetime(2026, 9, 19, tzinfo=timezone.utc))
        self.store.save(self.state)

    async def test_caller_sees_goal_and_observed_speech_not_grading_or_state(self):
        def answer(request):
            data = json.loads(request.content)
            self.assertEqual(data["max_tokens"], 400)
            self.assertEqual(data["response_format"], {"type": "json_object"})
            self.assertEqual(set(data["messages"][-1]), {"role", "content"})
            prompt = json.dumps(data["messages"])
            for private in ("fixture-adult", "fixture-child", "patient_id", "appointment_id", "expected", "operations", self.state.run_id):
                self.assertNotIn(private, prompt)
            self.assertIn("Lina Demo", prompt)
            self.assertIn("Please tell me your name", prompt)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": '{"text":"I am Lina Demo.","done":false}'}}], "usage": {"total_tokens": 20, "raw_secret": "do-not-store"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            caller = ModelCaller(self.config, self.store, self.state.run_id, client)
            result = await caller.reply("Please tell me your name", "en")
        self.assertEqual(result.text, "I am Lina Demo.")
        report = self.store.report(self.state.run_id)
        self.assertNotIn("do-not-store", json.dumps(report))
        self.assertNotIn("mock-secret", json.dumps(report))

    async def test_provider_failures_and_invalid_response_are_sanitized(self):
        for response in (httpx.Response(429, text="SECRET-BODY"), httpx.Response(200, json={"choices": []}),
                         httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "SECRET-BODY"}}]})):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _, r=response: r)) as client:
                caller = ModelCaller(self.config, self.store, self.state.run_id, client)
                with self.assertRaises((RuntimeError, ValueError, IndexError)):
                    await caller.reply("How can I help?", "en")
        report = self.store.report(self.state.run_id)
        self.assertNotIn("SECRET-BODY", json.dumps(report))
        errors = [e for e in report["events"] if e["kind"] == "provider_error"]
        self.assertEqual(len(errors), 3)
        self.assertIn("phase", errors[0]["payload"])
        self.assertIn("http_status", errors[0]["payload"])

    async def test_turn_limit_persists_a_nonpassing_failure_report(self):
        with patch.object(ModelCaller, "reply", new_callable=AsyncMock, return_value=CallerTurn(text="Hello")), patch.object(
            VercelInterpreter, "decide", new_callable=AsyncMock, return_value=TurnDecision(language="en", operations=[])
        ):
            report = await run_duel(self.config, self.store, max_turns=2)
        self.assertEqual(report["status"], "timed_out")
        self.assertFalse(report["fixture_grade"]["passed"])
        self.assertIn("limit_exhausted", report["failure_review"]["categories"])
        self.assertEqual(report["state"]["turn"], 2)
        self.assertEqual(report["manifest"]["execution"], "text_agent_duel")
        self.assertFalse(report["manifest"]["scripted_decisions"])
        self.assertEqual(report["manifest"]["oracle_visibility"], "evaluator_only")
        self.assertFalse(any(e["kind"] == "audio_output" for e in report["events"]))

    async def test_wall_clock_limit_and_cancellation_release_lease_and_save_outcome(self):
        async def slow(*_args):
            await asyncio.sleep(10)
        with patch.object(ModelCaller, "reply", side_effect=slow):
            report = await run_duel(self.config, self.store, max_seconds=0.01)
        self.assertEqual(report["status"], "timed_out")
        self.assertIn("limit_exhausted", report["failure_review"]["categories"])
        self.store.claim_experiment("next")
        self.store.release_experiment("next")
        entered = asyncio.Event()
        async def blocked(*_args):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(ModelCaller, "reply", side_effect=blocked):
            task = asyncio.create_task(run_duel(self.config, self.store))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        newest = self.store.list_runs(1)["runs"][0]
        self.assertEqual(newest["status"], "disconnected")
        self.assertTrue(any(e["kind"] == "failure_review" for e in self.store.report(newest["run_id"])["events"]))
        self.store.claim_experiment("after-cancel")
        self.store.release_experiment("after-cancel")

    async def test_early_caller_done_is_not_agent_completion(self):
        with patch.object(ModelCaller, "reply", new_callable=AsyncMock, return_value=CallerTurn(text="Goodbye", done=True)), patch.object(
            VercelInterpreter, "decide", new_callable=AsyncMock, return_value=TurnDecision(language="en", operations=[])
        ):
            report = await run_duel(self.config, self.store)
        self.assertEqual(report["status"], "disconnected")
        self.assertFalse(report["fixture_grade"]["passed"])
        self.assertEqual(report["state"]["turn"], 1)

    async def test_only_one_duel_per_persistent_run_store(self):
        self.store.claim_experiment("existing")
        other = RunStore(str(Path(self.folder.name) / "runs.db"))
        self.addCleanup(other.close)
        with self.assertRaises(RuntimeError):
            await run_duel(self.config, other)
        other.release_experiment("wrong-owner")
        with self.assertRaises(RuntimeError):
            other.claim_experiment("new")
        self.store.release_experiment("existing")
        other.claim_experiment("new")
        other.release_experiment("new")

    async def test_model_error_stops_agent_loop_without_additional_paid_retry(self):
        with patch.object(ModelCaller, "reply", new_callable=AsyncMock, return_value=CallerTurn(text="Please help")) as caller, patch.object(
            VercelInterpreter, "decide", new_callable=AsyncMock, side_effect=ValueError("mock invalid output")
        ):
            report = await run_duel(self.config, self.store)
        self.assertEqual(report["status"], "error")
        self.assertEqual(caller.await_count, 1)
        self.assertFalse(report["fixture_grade"]["passed"])
        self.assertIn("invalid_data", report["failure_review"]["categories"])

    async def test_invalid_limits_fail_before_any_provider_request(self):
        with patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("network forbidden")):
            for params in ({"max_turns": True}, {"max_turns": 9}, {"max_seconds": 0}, {"max_seconds": float("nan")},
                           {"max_seconds": 121}, {"language": "fr"}, {"scenario": "invalid"}, {"split": "invalid"}):
                with self.subTest(params=params), self.assertRaises(ValueError):
                    await run_duel(self.config, self.store, **params)
        for body in ({"text": " "}, {"text": "hello", "done": "false"}, {"text": "x" * 501}):
            with self.assertRaises(ValidationError):
                CallerTurn(**body)


if __name__ == "__main__":
    unittest.main()
