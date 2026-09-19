from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
from pydantic import Field

from v2.config import Config
from v2.models import CallState, Intent, Offer
from v2.providers import ProviderError, VercelInterpreter
from v2.store import BudgetExceeded, RunStore


def completion(content='{"language":"en","operations":[]}', **kwargs):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}], **kwargs})


class CarriedIntent(Intent):
    criteria: dict = Field(default_factory=dict)
    constraints: dict = Field(default_factory=dict)
    registration_fields: dict = Field(default_factory=dict)
    registration_inputs: dict = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    validation_errors: dict = Field(default_factory=dict)
    choices: list[dict] = Field(default_factory=list)
    identity_status: str = "verified"
    submission_uncertain: bool = False
    private_chart: dict = Field(default_factory=dict)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = RunStore()
        self.addCleanup(self.store.close)
        self.state = CallState(call_id="mock-gateway", reference_time=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.store.save(self.state)
        self.config = Config(allow_paid=True, gateway_key="test-key", llm_model="openai/gpt-4.1", mode="simulation")
        self.patch = patch("v2.providers.RETRY_DELAY_S", 0)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def interpreter(self, answer, config=None):
        http = httpx.AsyncClient(transport=httpx.MockTransport(answer), follow_redirects=True)
        self.addAsyncCleanup(http.aclose)
        return VercelInterpreter(config or self.config, self.store, http)

    def events(self, kind):
        return [event["payload"] for event in self.store.report(self.state.run_id)["events"] if event["kind"] == kind]

    async def test_strict_schema_no_actions_or_tools_and_sanitized_usage(self):
        def answer(request):
            body = json.loads(request.content)
            self.assertEqual(request.url, "https://ai-gateway.vercel.sh/v1/chat/completions")
            self.assertEqual(body["max_tokens"], 1800)
            self.assertNotIn("tools", body)
            output = body["response_format"]
            self.assertEqual(output["type"], "json_schema")
            self.assertTrue(output["json_schema"]["strict"])
            schema = output["json_schema"]["schema"]
            for node in [schema, *schema["$defs"].values()]:
                if node.get("type") == "object":
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                    self.assertFalse(node["additionalProperties"])
            return completion(usage={"total_tokens": 12, "prompt_tokens": True, "url": "PRIVATE",
                                     "outputTokens": {"total": 3, "secret": "PRIVATE"}})
        result = await self.interpreter(answer).decide("Hello", self.state)
        self.assertEqual(result.language, "en")
        self.assertEqual(self.events("llm_usage")[0]["usage"], {"total_tokens": 12, "outputTokens": {"total": 3}})
        self.assertEqual(self.store.budget()["committed_microusd"], 100_000)

    async def test_context_is_explicit_and_keeps_carried_inputs_and_offer_revision(self):
        self.state.intents["cancel"] = CarriedIntent(
            intent_id="cancel", action="cancel", subject="Demo", revision=3,
            identity_inputs={"name": "Demo", "national_id": "PRIVATE-ID", "secret": "PRIVATE"},
            patient={"patient_id": "PRIVATE-PATIENT", "notes": "PRIVATE-CHART", "upcoming": [{
                "appointment_id": "selectable", "provider_name": "Doctor Demo", "when": "tomorrow",
                "policy_id": "PRIVATE-POLICY"}]},
            criteria={"appointment_when": "Monday", "when": "Friday", "insurers": ["Plan Demo"],
                      "part_of_day": "afternoon", "clinician_language": "ca", "token": "PRIVATE"},
            constraints={"when": "Friday", "location_id": "norte", "patient_id": "PRIVATE"},
            registration_inputs={"given_name": "Demo", "token": "PRIVATE"},
            registration_fields={"first_surname": "Demo", "token": "PRIVATE"},
            missing_fields=["phone", "identity", "PRIVATE"], validation_errors={"phone": "invalid", "when": "PRIVATE"},
            choices=[{"doctor": "Doctor Demo", "secret": "PRIVATE"}], submission_uncertain=True,
            private_chart={"notes": "PRIVATE"},
            offers=[Offer(action="cancel", revision=2, presented_turn=1, payload={"secret": "PRIVATE"},
                          display={"when": "tomorrow", "doctor": "Doctor Demo", "url": "PRIVATE"})],
        )
        self.state.history = [{"role": "caller", "text": "Friday", "private_trace": "PRIVATE"},
                              {"role": "system", "text": "PRIVATE"}, {"role": "caller", "text": "Hello"}]
        def answer(request):
            body = json.loads(request.content)
            context = json.loads(body["messages"][1]["content"])
            self.assertNotIn("PRIVATE", json.dumps(context))
            intent = context["state"]["intents"]["cancel"]
            self.assertEqual(intent["constraints"], {"when": "Friday", "location_id": "norte"})
            self.assertEqual(intent["identity_fields"], {"name": "Demo"})
            self.assertEqual(intent["identity_collected"], ["name", "national_id"])
            self.assertEqual(intent["appointments"][0]["appointment_id"], "selectable")
            self.assertEqual(intent["offers"][0]["revision"], 2)
            self.assertFalse(intent["offers"][0]["presented"])
            self.assertEqual(intent["registration_inputs"], {"given_name": "Demo"})
            self.assertEqual(intent["registration_fields"], {"first_surname": "Demo"})
            self.assertEqual(intent["criteria"], {"appointment_when": "Monday", "when": "Friday", "insurers": ["Plan Demo"],
                                                "part_of_day": "afternoon", "clinician_language": "ca"})
            self.assertEqual(intent["validation_errors"], {"phone": "invalid", "when": "invalid"})
            self.assertEqual(intent["missing_fields"], ["phone", "identity"])
            self.assertEqual(intent["choices"], [{"doctor": "Doctor Demo"}])
            self.assertEqual(intent["identity_status"], "verified")
            self.assertTrue(intent["submission_uncertain"])
            self.assertEqual(context["state"]["recent_dialogue"], [{"role": "caller", "text": "Friday"}])
            return completion()
        await self.interpreter(answer).decide("Hello", self.state)

    async def test_retry_has_separate_reservations_and_bounded_diagnostics(self):
        count = 0
        def answer(_):
            nonlocal count
            count += 1
            return httpx.Response(429, text="PRIVATE https://token.test", headers={"retry-after": "0"}) if count == 1 else completion()
        await self.interpreter(answer).decide("Hello", self.state)
        self.assertEqual(count, 2)
        error = self.events("provider_error")[0]
        self.assertEqual((error["type"], error["status"], error["phase"]), ("HTTPStatusError", 429, "request"))
        self.assertIsInstance(error["elapsed_ms"], int)
        self.assertNotEqual(error["reservation"], self.events("llm_usage")[0]["reservation"])
        self.assertEqual(self.store.budget()["committed_microusd"], 200_000)
        self.assertNotIn("PRIVATE", json.dumps(self.events("provider_error")))

    async def test_auth_redirect_and_long_retry_after_do_not_retry(self):
        for status, headers in [(401, {}), (403, {}), (400, {}), (302, {"location": "https://private.test"}),
                                (429, {"retry-after": "120"})]:
            with self.subTest(status=status):
                count = 0
                def answer(_):
                    nonlocal count
                    count += 1
                    return httpx.Response(status, text="PRIVATE", headers=headers)
                with self.assertRaises(ProviderError) as error:
                    await self.interpreter(answer).decide("Hello", self.state)
                self.assertNotIn("PRIVATE", str(error.exception))
                self.assertEqual(count, 1)

    async def test_transport_errors_are_retried_but_never_leaked(self):
        for error in [httpx.ReadTimeout("PRIVATE https://key.test"), httpx.ConnectError("PRIVATE")]:
            def answer(_):
                raise error
            before = len(self.events("provider_error"))
            with self.assertRaises(ProviderError) as caught:
                await self.interpreter(answer).decide("Hello", self.state)
            self.assertEqual(len(self.events("provider_error")) - before, 2)
            self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertNotIn("PRIVATE", json.dumps(self.events("provider_error")))

    async def test_malformed_incomplete_and_non_strict_decisions_fail_closed(self):
        responses = [
            httpx.Response(200, text="PRIVATE not-json"), httpx.Response(200, json=[]),
            httpx.Response(200, json={"choices": []}),
            httpx.Response(200, json={"choices": [{"message": {"content": '{"language":"en","operations":[]}'}}]}),
            httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}),
            completion('{"language":"en"}'), completion('{"language":"en","language":"es","operations":[]}'),
            completion('{"language":"en","operations":[{"op":"confirm","option":"1"}]}'),
            completion('{"language":"en","operations":[],"token":"PRIVATE"}'),
            completion('{"language":"en","operations":NaN}'),
        ]
        for response in responses:
            with self.subTest(body=response.content[:40]):
                with self.assertRaises(ProviderError):
                    await self.interpreter(lambda _: response).decide("Hello", self.state)
        self.assertNotIn("PRIVATE", json.dumps(self.events("provider_error")))

    async def test_response_and_unicode_context_bounds(self):
        with self.assertRaises(ProviderError):
            await self.interpreter(lambda _: httpx.Response(200, text="x" * 32_001)).decide("Hello", self.state)
        before = self.store.budget()["committed_microusd"]
        def forbidden(_):
            raise AssertionError("network forbidden")
        provider = self.interpreter(forbidden)
        for text in ["", " ", "x" * 2001]:
            with self.assertRaises(ValueError):
                await provider.decide(text, self.state)
        self.state.history = [{"role": "caller", "text": "界" * 2000}] * 8
        with self.assertRaises(ValueError):
            await provider.decide("Hello", self.state)
        self.assertEqual(self.store.budget()["committed_microusd"], before)

    async def test_budget_is_checked_before_every_attempt(self):
        limited = RunStore(cap_microusd=100_000)
        self.addCleanup(limited.close)
        limited.save(self.state)
        provider = self.interpreter(lambda _: httpx.Response(503))
        provider.store = limited
        with self.assertRaises(BudgetExceeded):
            await provider.decide("Hello", self.state)
        self.assertEqual(limited.budget()["committed_microusd"], 100_000)

    async def test_total_attempt_deadline_and_cancellation(self):
        entered = asyncio.Event()
        async def stalled(_):
            entered.set()
            await asyncio.Event().wait()
        provider = self.interpreter(stalled)
        with patch("v2.providers.GATEWAY_TIMEOUT_S", 0.01):
            with self.assertRaises(ProviderError):
                await provider.decide("Hello", self.state)
        self.assertEqual([event["type"] for event in self.events("provider_error")], ["TimeoutError"] * 2)
        entered.clear()
        task = asyncio.create_task(provider.decide("Hello", self.state))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events("provider_error")[-1]["type"], "CancelledError")
        self.assertEqual(self.store.budget()["committed_microusd"], 300_000)

    async def test_close_is_idempotent_and_closed_or_disabled_clients_do_not_reserve(self):
        provider = self.interpreter(lambda _: completion())
        await asyncio.gather(provider.close(), provider.close())
        await provider.close()
        self.assertTrue(provider.client.is_closed)
        with self.assertRaises(RuntimeError):
            await provider.decide("Hello", self.state)
        with self.assertRaises(PermissionError):
            await self.interpreter(lambda _: completion(), replace(self.config, allow_paid=False)).decide("Hello", self.state)
        self.assertEqual(self.store.budget()["committed_microusd"], 0)

    async def test_failed_close_is_sanitized_and_can_be_retried(self):
        provider = self.interpreter(lambda _: completion())
        original = provider.client.aclose
        attempts = 0
        async def flaky_close():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("PRIVATE")
            await original()
        with patch.object(provider.client, "aclose", flaky_close):
            with self.assertRaises(ProviderError) as error:
                await provider.close()
            self.assertNotIn("PRIVATE", str(error.exception))
            await provider.close()
        self.assertEqual(attempts, 2)
        self.assertTrue(provider.client.is_closed)

    async def test_cancelled_close_still_finishes_transport_cleanup(self):
        provider = self.interpreter(lambda _: completion())
        original = provider.client.aclose
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_close():
            entered.set()
            await release.wait()
            await original()
        with patch.object(provider.client, "aclose", slow_close):
            task = asyncio.create_task(provider.close())
            await entered.wait()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(provider.client.is_closed)
