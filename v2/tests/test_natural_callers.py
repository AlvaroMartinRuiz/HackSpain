from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from v2.clinic import Dispatcher, FixtureClinic
from v2.domain_rules import confirmation_selection
from v2.models import CallState
from v2.store import RunStore
from v2.tests.test_workflow import NOW, decision
from v2.workflow import CallController


class NaturalConsentTests(unittest.TestCase):
    def test_natural_acceptance_in_three_languages(self):
        for text, option in [
            ("Yes", None), ("Yes, option two", 2), ("Yes please", None), ("Yes, the first one works for me.", 1),
            ("Sí, la primera opción me va bien.", 1), ("Vale, la segunda, por favor", 2), ("Sí, la opción 2", 2),
            ("Perfecto, resérvemela", None), ("Sí, a las nueve y cuarto me va bien", None),
            ("D'acord, la primera", 1), ("Ok, the 9:15 one", None),
            # As speech-to-text actually wrote them on a live call.
            ("Yes. The 1st 1 works for me.", 1), ("Yes, the 2nd one", 2), ("Sí, la 1a", 1),
        ]:
            with self.subTest(text=text):
                self.assertEqual(confirmation_selection(text)[:2], (True, option))

    def test_qualified_or_questioning_replies_are_not_consent(self):
        for text in ["Yes but not that doctor", "Sí, pero prefiero por la tarde", "¿Hay algo más temprano?",
                     "No, nada más, gracias.", "Maybe the first one", "The first one or the second, I don't mind",
                     "Espere, mejor otra fecha", "I have a headache", "Yes, the first or the second"]:
            with self.subTest(text=text):
                self.assertFalse(confirmation_selection(text)[0])


SAID = "I'm Lina Demo, born 1990-01-01, I need a GP tomorrow"
WHO = {"name": "Lina Demo", "date_of_birth": "1990-01-01"}


class CompressedDecisionTests(unittest.IsolatedAsyncioTestCase):
    """Shapes the live model actually returned for a caller who says everything at once."""

    def setUp(self):
        self.store = RunStore()
        self.addCleanup(self.store.close)
        self.state = CallState(call_id="compressed", reference_time=NOW)
        self.controller = CallController(self.state, FixtureClinic(), self.store,
                                         Dispatcher(self.store, "simulation", AsyncMock()))

    def assert_offered(self, reply, intent_id):
        intent = self.state.intents[intent_id]
        self.assertIsNotNone(intent.patient)
        self.assertEqual(intent.status, "awaiting_confirmation")
        self.assertTrue(intent.offers)
        self.assertNotIn("date of birth", reply.text.lower())
        self.assertEqual(reply.text.count("confirm the exact option"), 1)

    async def test_everything_on_create(self):
        reply = await self.controller.turn(SAID, decision({
            "op": "create", "intent_id": "mine", "action": "book", "subject": "Lina Demo", "evidence": SAID,
            "identity": WHO, "specialty_id": "general_practice", "when": "tomorrow"}))
        self.assert_offered(reply, "mine")

    async def test_no_create_and_the_action_on_ask(self):
        reply = await self.controller.turn(SAID, decision(
            {"op": "identify", "intent_id": "1", "identity": WHO, "evidence": SAID},
            {"op": "ask", "question": "appointment", "intent_id": "1", "action": "book",
             "specialty_id": "general_practice", "when": "tomorrow", "evidence": SAID}))
        self.assert_offered(reply, "1")

    async def test_no_action_stated_for_a_named_specialty_is_a_booking(self):
        reply = await self.controller.turn(SAID, decision(
            {"op": "identify", "intent_id": "1", "identity": WHO, "evidence": SAID},
            {"op": "ask", "question": "appointment", "intent_id": "1",
             "specialty_id": "general_practice", "when": "tomorrow", "evidence": SAID}))
        self.assert_offered(reply, "1")

    async def test_no_action_is_never_guessed_for_a_cancellation(self):
        said = "I'm Lina Demo, born 1990-01-01, cancel my GP appointment"
        await self.controller.turn(said, decision(
            {"op": "identify", "intent_id": "1", "identity": WHO, "evidence": said},
            {"op": "ask", "intent_id": "1", "specialty_id": "general_practice", "evidence": said}))
        self.assertEqual(self.state.intents, {})

    async def test_a_confirm_that_repeats_the_criteria_does_not_prepare_again(self):
        reply = await self.controller.turn(SAID, decision({
            "op": "create", "intent_id": "mine", "action": "book", "subject": "Lina Demo", "evidence": SAID,
            "identity": WHO, "specialty_id": "general_practice", "when": "tomorrow"}))
        self.controller.presented(reply)
        yes = "Yes, the first one works for me"
        await self.controller.turn(yes, decision({
            "op": "confirm", "intent_id": "mine", "option": 1, "offer_revision": self.state.intents["mine"].revision,
            "specialty_id": "general_practice", "when": "tomorrow", "evidence": yes}))
        self.assertEqual(self.state.intents["mine"].status, "completed")
        self.assertEqual(len(self.state.intents["mine"].receipts), 1)


class DictatedDateTests(unittest.TestCase):
    def test_numeric_dates_are_spelled_in_the_convention_they_were_heard_in(self):
        from v2.domain_rules import written_dates
        # English speech-to-text wrote "the seventh of August 1962" as 08/07/1962 on a live call.
        self.assertEqual(written_dates("born on the 08/07/1962.", "en"), "born on the 7 August 1962.")
        self.assertEqual(written_dates("nací el 07/08/1962", "es"), "nací el 7 de agosto de 1962")
        self.assertEqual(written_dates("born 25/12/1990", "en"), "born 25 December 1990")
        self.assertEqual(written_dates("born 31/02/1990", "en"), "born 31/02/1990")
        self.assertEqual(written_dates("no dates here", "en"), "no dates here")


class PausingCallerTests(unittest.IsolatedAsyncioTestCase):
    """A caller who pauses mid-sentence reaches the controller as fragments; the voice
    pipeline cancels the unanswered one when the next fragment arrives."""

    async def asyncSetUp(self):
        self.store = RunStore()
        self.addCleanup(self.store.close)
        self.state = CallState(call_id="pausing", language="en", reference_time=NOW)
        self.heard: list[str] = []
        self.decisions: list = []
        controller = self

        class Interpreter:
            async def decide(self, text, state):
                controller.heard.append(text)
                if not controller.decisions:
                    await asyncio.Event().wait()  # still thinking when the next fragment lands
                return controller.decisions.pop(0)

        self.controller = CallController(self.state, FixtureClinic(), self.store,
                                         Dispatcher(self.store, "simulation", AsyncMock()), Interpreter())

    async def fragment_then_rest(self, first, rest, answer):
        pending = asyncio.create_task(self.controller.turn(first, expected_epoch=self.controller.epoch))
        while not self.heard:
            await asyncio.sleep(0)
        self.controller.interrupt(source="caller_input")
        self.assertIsNone(await pending)
        self.decisions.append(answer)
        return await self.controller.turn(rest, expected_epoch=self.controller.epoch)

    async def test_an_unanswered_fragment_is_heard_with_the_rest(self):
        first, rest = "Hi, this is Lina Demo, born 1990-01-01.", "I need a GP tomorrow."
        reply = await self.fragment_then_rest(first, rest, decision({
            "op": "create", "intent_id": "mine", "action": "book", "subject": "Lina Demo",
            "evidence": "Lina Demo, born 1990-01-01", "identity": WHO,
            "specialty_id": "general_practice", "when": "tomorrow"}))
        self.assertEqual(self.heard[-1], f"{first} {rest}")
        self.assertEqual(self.state.intents["mine"].status, "awaiting_confirmation")
        self.assertTrue(reply.offers)

    async def test_language_is_not_locked_by_fragments_that_were_never_answered(self):
        # No word-level cue either way: only the model's reading of the language can decide it,
        # and that is only trusted until the call's language is established.
        first, rest = "Lina Demo,", "1990-01-01. Medicina general."
        reply = await self.fragment_then_rest(first, rest, decision(
            {"op": "ask", "question": "identity", "evidence": "Lina Demo"}, language="es"))
        self.assertGreater(self.state.turn, 1)
        self.assertEqual(self.state.language, "es")
        self.assertEqual(reply.language, "es")

    def test_the_greeting_invites_both_languages(self):
        from v2.workflow import TEXT
        self.assertIn("buenos días", TEXT["en"]["hello"])


if __name__ == "__main__":
    unittest.main()
