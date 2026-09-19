from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from v2.api import create_app
from v2.config import Config
from v2.evaluation import demo_request, fixture_grade
from v2.models import CallState
from v2.providers import JevClient, VercelInterpreter
from v2.store import RunStore
from v2.tests.test_workflow import NOW


class APITests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = Config(operator_token="test-operator", allow_paid=False, mode="simulation",
                             data_dir=Path(self.folder.name), public_operator_tools=False, public_carrier_calls=False)
        self.headers = {"X-V2-Token": "test-operator"}

    def test_rehearsal_http_path_is_offline_and_exposes_separate_metrics(self):
        with patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("network forbidden")):
            with TestClient(create_app(self.config)) as client:
                self.assertEqual(client.get("/api/budget").status_code, 401)
                response = client.post("/api/rehearse", headers=self.headers, json=demo_request().model_dump())
                self.assertEqual(response.status_code, 200)
                report = response.json()
                self.assertEqual(report["metrics"]["resolved"], 2)
                self.assertEqual(report["metrics"]["simulated_receipts"], 2)
                self.assertEqual(report["metrics"]["http_accepted"], 0)
                self.assertIsNone(report["official_grade"])
                self.assertEqual(report["metrics"]["audio_status"], "not_measured")
                budget = client.get("/api/budget", headers=self.headers).json()
                self.assertEqual(budget["committed_microusd"], 0)
                self.assertEqual(client.get('/api/runs/' + report["run_id"], headers=self.headers).status_code, 200)
                grade = fixture_grade(report, [("cancel", "fixture-appointment-fixture-adult"), ("book", "fixture-child")])
                self.assertTrue(grade["passed"])

    def test_live_cutover_is_locked_and_paid_voice_is_not_ready(self):
        with self.assertRaises(ValueError):
            create_app(replace(self.config, mode="live", allow_submissions=True))
        with TestClient(create_app(self.config)) as client:
            health = client.get("/health").json()
            self.assertFalse(health["voice_ready"])
            self.assertFalse(health["live_cutover_enabled"])
            self.assertNotIn("test-operator", json.dumps(health))
            body = demo_request().model_dump()
            body["mode"] = "live"
            self.assertEqual(client.post("/api/rehearse", headers=self.headers, json=body).status_code, 422)

    def test_secrets_are_not_in_config_representation(self):
        config = replace(self.config, gateway_key="SECRET-ONE", cartesia_key="SECRET-TWO")
        self.assertNotIn("SECRET", repr(config))
        self.assertNotIn("SECRET", json.dumps(config.manifest()))


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = RunStore()
        self.addCleanup(self.store.close)
        self.state = CallState(call_id="provider-test", reference_time=NOW)
        self.store.save(self.state)
        self.config = Config(allow_paid=True, gateway_key="test-key", operator_token="test", mode="simulation",
                             jev_url="https://jev.test/api/evaluate", jev_token="service-test")

    async def test_interpreter_is_bounded_and_requests_typed_output(self):
        def answer(request):
            body = json.loads(request.content)
            self.assertEqual(body["response_format"]["type"], "json_schema")
            self.assertTrue(body["response_format"]["json_schema"]["strict"])
            self.assertLessEqual(body["max_tokens"], 1800)
            self.assertNotIn("tools", body)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": '{"language":"en","operations":[]}'}}], "usage": {"total_tokens": 50}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            result = await VercelInterpreter(self.config, self.store, http).decide("Hello", self.state)
        self.assertEqual(result.language, "en")
        self.assertEqual(self.store.budget()["committed_microusd"], 100_000)

    async def test_provider_errors_do_not_store_raw_responses(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403, text="SECRET KEY"))) as http:
            with self.assertRaises(RuntimeError):
                await VercelInterpreter(self.config, self.store, http).decide("Hello", self.state)
        self.assertNotIn("SECRET", json.dumps(self.store.report(self.state.run_id)))
        self.assertEqual(self.store.budget()["committed_microusd"], 100_000)

    async def test_no_paid_flag_means_no_provider_request(self):
        def forbidden(_request):
            raise AssertionError("network must not be used")
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
            with self.assertRaises(PermissionError):
                await VercelInterpreter(replace(self.config, allow_paid=False), self.store, http).decide("Hello", self.state)
        self.assertEqual(self.store.budget()["committed_microusd"], 0)

    async def test_jev_assessments_do_not_become_official_grades(self):
        def answer(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["question_pack"], "conversation-v1")
            return httpx.Response(200, json={"schema_version": 1, "source": "model_assessment",
                                            "model": "typesafe-ai/jev", "official_grade": None,
                                            "run_id": payload["run_id"], "question_pack": "conversation-v1",
                                            "answers": {name: {"type": "boolean", "probability": 0.1}
                                                        for name in ("unanswered_request", "repeated_question", "premature_success")}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            await JevClient(self.config, self.store, http).evaluate(self.state.run_id, [{"role": "caller", "text": "Hello"}])
        assessment = self.store.report(self.state.run_id)["events"][-1]
        self.assertEqual(assessment["kind"], "model_assessment")
        self.assertIsNone(assessment["payload"]["official_grade"])
        self.assertEqual(self.store.budget()["committed_microusd"], 10_000)


if __name__ == "__main__":
    unittest.main()
