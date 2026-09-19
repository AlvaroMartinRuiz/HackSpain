from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError

from v2.clinic import Dispatcher, FixtureClinic
from v2.models import CallState, Intent, Operation, TurnDecision
from v2.store import RunStore
from v2.workflow import CallController

NOW = datetime.fromisoformat("2026-09-19T09:00:00+02:00")


def decision(*operations, language="en"):
    return TurnDecision(language=language, operations=[Operation(**op) for op in operations])


def booking(intent="mine", name="Lina Demo", born="1990-01-01"):
    return decision(
        {"op": "create", "intent_id": intent, "action": "book", "subject": name, "evidence": name},
        {"op": "identify", "intent_id": intent, "identity": {"name": name, "date_of_birth": born}, "evidence": name},
        {"op": "prepare", "intent_id": intent, "specialty_id": "general_practice", "when": "tomorrow", "evidence": name},
    )


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = RunStore()
        self.addCleanup(self.store.close)
        self.state = CallState(call_id="test", reference_time=NOW)
        self.sender = AsyncMock()
        self.controller = CallController(self.state, FixtureClinic(), self.store,
                                         Dispatcher(self.store, "simulation", self.sender))

    async def offer(self):
        return await self.controller.turn("Please book for Lina Demo, born 1990-01-01", booking())

    async def confirm(self, revision=1, text="Yes, option one"):
        return await self.controller.turn(text, decision({"op": "confirm", "intent_id": "mine", "option": 1,
                                                          "offer_revision": revision, "evidence": "Yes"}))

    async def test_offer_must_be_presented_before_acceptance(self):
        await self.offer()
        await self.confirm()
        self.assertEqual(self.state.intents["mine"].receipts, [])
        self.sender.assert_not_called()

    async def test_simulation_never_calls_a_live_sender(self):
        self.controller.presented(await self.offer())
        await self.confirm()
        receipt = self.state.intents["mine"].receipts[0]
        self.assertTrue(receipt["accepted"])
        self.assertIsNone(receipt["http_status"])
        self.assertEqual(receipt["source"], "simulation")
        self.sender.assert_not_called()
        self.assertIsNone(self.store.report(self.state.run_id)["official_grade"])

    async def test_changed_offer_invalidates_old_confirmation(self):
        self.controller.presented(await self.offer())
        reply = await self.controller.turn("Make it next Thursday instead", decision({
            "op": "prepare", "intent_id": "mine", "specialty_id": "general_practice",
            "when": "next Thursday", "evidence": "next Thursday",
        }))
        self.controller.presented(reply)
        await self.confirm(revision=1)
        self.assertFalse(self.state.intents["mine"].receipts)
        await self.confirm(revision=2)
        self.assertEqual(len(self.state.intents["mine"].receipts), 1)

    async def test_interrupted_offer_is_not_presented(self):
        reply = await self.offer()
        self.controller.interrupt()
        self.controller.presented(reply)
        await self.confirm()
        self.assertFalse(self.state.intents["mine"].receipts)

    async def test_yes_but_is_not_consent(self):
        self.controller.presented(await self.offer())
        await self.confirm(text="Yes but not that doctor")
        self.assertFalse(self.state.intents["mine"].receipts)

    async def test_unverified_identity_cannot_produce_an_offer(self):
        await self.controller.turn("Please book for Lina Demo", booking(born="1991-01-01"))
        self.assertIsNone(self.state.intents["mine"].patient)
        self.assertFalse(self.state.intents["mine"].offers)

    async def test_multi_intent_requires_every_request_resolved(self):
        self.controller.presented(await self.offer())
        await self.confirm()
        reply = await self.controller.turn("Also book for Roc Demo, born 2018-01-01", booking("child", "Roc Demo", "2018-01-01"))
        self.controller.presented(reply)
        result = await self.controller.turn("Thank you", decision({"op": "finish"}))
        self.assertFalse(result.completion)
        await self.controller.turn("Yes, option one", decision({"op": "confirm", "intent_id": "child",
                                                                "option": 1, "offer_revision": 1, "evidence": "Yes"}))
        result = await self.controller.turn("Thank you", decision({"op": "finish"}))
        self.assertTrue(result.completion)
        self.assertEqual(self.state.intents["mine"].receipts[0]["payload"]["patient_id"], "fixture-adult")
        self.assertEqual(self.state.intents["child"].receipts[0]["payload"]["patient_id"], "fixture-child")

    async def test_emergency_escalates_without_booking(self):
        await self.controller.turn("I have chest pain", decision())
        receipt = self.state.intents["urgent"].receipts[0]
        self.assertEqual(receipt["action"], "escalate")
        self.assertEqual(receipt["payload"]["reason"], "medical_emergency")
        self.sender.assert_not_called()

    async def test_bad_evidence_cannot_create_intents(self):
        await self.controller.turn("Hello", booking())
        self.assertFalse(self.state.intents)

    async def test_interrupt_cancels_interpretation(self):
        started = asyncio.Event()
        async def interpret(*_args):
            started.set()
            await asyncio.Event().wait()
        self.controller.interpreter = SimpleNamespace(decide=interpret)
        task = asyncio.create_task(self.controller.turn("Please book a doctor"))
        await started.wait()
        self.controller.interrupt()
        self.assertIsNone(await asyncio.wait_for(task, 1))

    async def test_catalan_and_spanish_templates(self):
        for text, language, word in [("Hola, vull una cita", "ca", "pacient"),
                                     ("Hola, quiero una cita", "es", "paciente")]:
            state = CallState(call_id=language, reference_time=NOW, language=language)
            controller = CallController(state, FixtureClinic(), self.store, Dispatcher(self.store, "simulation"))
            reply = await controller.turn(text, decision(language=language))
            self.assertEqual(reply.language, language)
            self.assertIn(word, reply.text)


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = RunStore()
        self.addCleanup(self.store.close)
        self.state = CallState(call_id="call", mode="live", reference_time=NOW)
        self.intent = Intent(intent_id="one", action="cancel", subject="test")

    async def test_pending_outcome_cannot_be_replayed_blindly(self):
        sender = AsyncMock(side_effect=TimeoutError())
        sink = Dispatcher(self.store, "live", sender, allow_live=True)
        with self.assertRaises(TimeoutError):
            await sink.execute(self.state, self.intent, "cancel", {"appointment_id": "known"})
        with self.assertRaises(RuntimeError):
            await sink.execute(self.state, self.intent, "cancel", {"appointment_id": "known"})
        self.assertEqual(sender.await_count, 1)

    async def test_cancellation_preserves_inflight_receipt(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def sender(*_args):
            entered.set()
            await release.wait()
            return SimpleNamespace(accepted=True, duplicate=False, status=200)
        sink = Dispatcher(self.store, "live", sender, allow_live=True)
        task = asyncio.create_task(sink.execute(self.state, self.intent, "cancel", {"appointment_id": "known"}))
        await entered.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = await sink.execute(self.state, self.intent, "cancel", {"appointment_id": "known"})
        self.assertTrue(result["accepted"])

    async def test_model_cannot_change_sink_mode(self):
        with self.assertRaises(ValueError):
            await Dispatcher(self.store, "simulation").execute(self.state, self.intent, "cancel", {})


class SchemaTests(unittest.TestCase):
    def test_untrusted_fields_and_naive_clock_are_rejected(self):
        with self.assertRaises(ValidationError):
            Operation(op="prepare", patient_id="invented")
        with self.assertRaises(ValidationError):
            CallState(call_id="call", reference_time=datetime(2026, 9, 19))


if __name__ == "__main__":
    unittest.main()
