from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from v2.config import Config
from v2.evaluation import demo_request, fixture_grade, rehearse
from v2.models import CallState
from v2.simulator import CallerTurn, ModelCaller, run_duel
from v2.providers import VercelInterpreter
from v2.store import RunStore
from v2.tests.test_workflow import NOW, booking, decision
from v2.clinic import Dispatcher, FixtureClinic
from v2.workflow import CallController

EXPECTED = [("cancel", "fixture-appointment-fixture-adult"), ("book", "fixture-child")]


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_passes_in_all_three_languages(self):
        store = RunStore()
        self.addCleanup(store.close)
        for language in ("es", "en", "ca"):
            report = await rehearse(demo_request(language), store)
            self.assertTrue(fixture_grade(report, EXPECTED)["passed"], language)
            self.assertEqual(report["state"]["language"], language)
            self.assertEqual(report["metrics"]["audio_status"], "not_measured")

    async def test_two_agent_loop_with_mocked_models_uses_only_simulated_actions(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = RunStore(str(Path(folder.name) / "runs.db"))
        self.addCleanup(store.close)
        turns = demo_request().turns
        config = Config(allow_paid=True, gateway_key="fake", mode="simulation", data_dir=Path(folder.name))
        with patch.object(ModelCaller, "reply", new_callable=AsyncMock,
                          side_effect=[CallerTurn(text=t.text) for t in turns]) as caller, patch.object(
            VercelInterpreter, "decide", new_callable=AsyncMock, side_effect=[t.decision for t in turns]
        ):
            report = await run_duel(config, store)
        self.assertTrue(report["fixture_grade"]["passed"])
        self.assertEqual(report["metrics"]["http_accepted"], 0)
        self.assertEqual(report["metrics"]["simulated_receipts"], 2)
        self.assertEqual(report["manifest"]["execution"], "text_agent_duel")
        self.assertTrue(all(isinstance(call.args[0], str) for call in caller.call_args_list))

    async def test_live_simulation_requires_opt_in_and_persistent_store(self):
        store = RunStore()
        self.addCleanup(store.close)
        with self.assertRaises(PermissionError):
            await run_duel(Config(allow_paid=False), store)
        with self.assertRaises(ValueError):
            await run_duel(Config(allow_paid=True, gateway_key="fake"), store)

    async def test_emergency_does_not_depend_on_the_language_model(self):
        store = RunStore()
        self.addCleanup(store.close)
        state = CallState(call_id="urgent", reference_time=NOW)
        controller = CallController(state, FixtureClinic(), store, Dispatcher(store, "simulation"))
        await controller.turn("I have chest pain")
        self.assertTrue(state.emergency)
        await controller.turn("Please book for Lina Demo", booking())
        self.assertNotIn("mine", state.intents)
        receipts = [e for e in store.report(state.run_id)["events"] if e["kind"] == "action_receipt"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["payload"]["action"], "escalate")

    async def test_old_queued_turn_is_discarded_after_interruption(self):
        store = RunStore()
        self.addCleanup(store.close)
        controller = CallController(CallState(call_id="queued", reference_time=NOW), FixtureClinic(), store,
                                    Dispatcher(store, "simulation"))
        controller.interrupt()
        result = await controller.turn("Please book for Lina Demo", booking(), expected_epoch=0)
        self.assertIsNone(result)
        self.assertFalse(controller.state.intents)


if __name__ == "__main__":
    unittest.main()
