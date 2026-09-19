from __future__ import annotations

import asyncio
import copy
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone

import httpx

from v2.config import Config
from v2.models import CallState
from v2.providers import JevClient, ProviderError
from v2.store import RunStore


class JevClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = RunStore()
        self.addCleanup(self.store.close)
        self.state = CallState(call_id="jev-mock", reference_time=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.store.save(self.state)
        self.config = Config(allow_paid=True, mode="simulation", jev_url="https://jev.test/api/evaluate", jev_token="test-token")
        self.turns = [{"role": "caller", "text": "Hello"}]
        self.result = {"schema_version": 1, "run_id": self.state.run_id, "question_pack": "conversation-v1",
                       "source": "model_assessment", "model": "typesafe-ai/jev", "official_grade": None,
                       "answers": {name: {"type": "boolean", "probability": 0.1}
                                   for name in ("unanswered_request", "repeated_question", "premature_success")}}

    def adapter(self, answer, config=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(answer), follow_redirects=True)
        self.addAsyncCleanup(client.aclose)
        return JevClient(config or self.config, self.store, client)

    def events(self):
        return self.store.report(self.state.run_id)["events"]

    async def test_correlated_assessment_has_fixed_schema_and_sanitized_usage(self):
        def answer(request):
            self.assertEqual(request.headers["authorization"], "Bearer test-token")
            payload = json.loads(request.content)
            self.assertEqual(set(payload), {"schema_version", "run_id", "question_pack", "turns"})
            self.assertEqual(payload["question_pack"], "conversation-v1")
            return httpx.Response(200, json={**self.result, "usage": {"totalTokens": 12, "token": "PRIVATE"}})
        result = await self.adapter(answer).evaluate(self.state.run_id, self.turns)
        self.assertEqual(result["usage"], {"totalTokens": 12})
        self.assertIsNone(result["official_grade"])
        self.assertEqual(self.events()[-1]["kind"], "model_assessment")
        self.assertIsNone(self.events()[-1]["payload"]["official_grade"])
        self.assertNotIn("PRIVATE", json.dumps(result) + json.dumps(self.events()))

    async def test_invalid_input_never_calls_the_provider(self):
        def forbidden(_):
            raise AssertionError("network forbidden")
        adapter = self.adapter(forbidden)
        for turns in [None, {}, [], [None], [{}], [{"role": "system", "text": "Hello"}],
                      [{"role": "caller", "text": " "}], [{"role": "caller", "text": 1}],
                      [{"role": "caller", "text": "a" * 1001}], self.turns * 41,
                      [{"role": "caller", "text": "界" * 1000}] * 9,
                      [{"role": "caller", "text": "Hello", "private": "unselected"}]]:
            with self.subTest(turns=str(turns)[:30]):
                with self.assertRaises(ValueError):
                    await adapter.evaluate(self.state.run_id, turns)
        with self.assertRaises(ValueError):
            await adapter.evaluate("invalid/id", self.turns)
        with self.assertRaises(PermissionError):
            await self.adapter(forbidden, replace(self.config, allow_paid=False)).evaluate(self.state.run_id, self.turns)

    async def test_config_endpoint_rejects_query_credentials_and_redirects(self):
        for url in ["https://jev.test/api?secret=private", "https://token@jev.test/api", "https://jev.test/api#token"]:
            with self.assertRaises(ValueError):
                await self.adapter(lambda _: None, replace(self.config, jev_url=url)).evaluate(self.state.run_id, self.turns)
        calls = 0
        def redirect(_):
            nonlocal calls
            calls += 1
            return httpx.Response(307, headers={"location": "https://other.test/secret"})
        with self.assertRaises(ProviderError):
            await self.adapter(redirect).evaluate(self.state.run_id, self.turns)
        self.assertEqual(calls, 1)

    async def test_malformed_probabilities_and_correlation_fail_closed(self):
        invalid = []
        for key, value in [("run_id", "another"), ("schema_version", True), ("question_pack", "another"),
                           ("source", "official"), ("model", "another"), ("official_grade", True)]:
            invalid.append({**self.result, key: value})
        invalid.extend([[], {}, {**self.result, "secret": "PRIVATE"}])
        for probability in [True, None, "0.1", -1, 1.1]:
            result = copy.deepcopy(self.result)
            result["answers"]["unanswered_request"]["probability"] = probability
            invalid.append(result)
        result = copy.deepcopy(self.result)
        result["answers"]["unanswered_request"]["private"] = "PRIVATE"
        invalid.append(result)
        result = copy.deepcopy(self.result)
        result["answers"].pop("repeated_question")
        invalid.append(result)
        for result in invalid:
            with self.subTest(result=result):
                with self.assertRaises(ProviderError):
                    await self.adapter(lambda _: httpx.Response(200, json=result)).evaluate(self.state.run_id, self.turns)
        self.assertTrue(all(event["kind"] == "provider_error" for event in self.events()))
        self.assertNotIn("PRIVATE", json.dumps(self.events()))

    async def test_nonfinite_duplicate_or_oversized_output_is_rejected(self):
        for body in [json.dumps(self.result).replace('0.1', 'NaN'),
                     json.dumps(self.result).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
                     "x" * 32_001]:
            with self.assertRaises(ProviderError):
                await self.adapter(lambda _: httpx.Response(200, text=body)).evaluate(self.state.run_id, self.turns)

    async def test_provider_failures_and_cancellation_are_recorded_without_retry(self):
        for status in [401, 429, 500]:
            with self.assertRaises(ProviderError) as error:
                await self.adapter(lambda _: httpx.Response(status, text="PRIVATE token and URL")).evaluate(self.state.run_id, self.turns)
            self.assertNotIn("PRIVATE", str(error.exception))
            diagnostic = self.events()[-1]["payload"]
            self.assertEqual(diagnostic["status"], status)
            self.assertEqual(diagnostic["attempt"], 1)
            self.assertFalse(diagnostic["retrying"])
        entered = asyncio.Event()
        async def stalled(_):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(self.adapter(stalled).evaluate(self.state.run_id, self.turns))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events()[-1]["payload"]["type"], "CancelledError")
        self.assertNotIn("PRIVATE", json.dumps(self.events()))
