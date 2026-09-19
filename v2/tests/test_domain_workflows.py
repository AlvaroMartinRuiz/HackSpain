from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from v2.clinic import Dispatcher, FixtureClinic
from v2.models import CallState, Operation, TurnDecision
from v2.store import RunStore
from v2.workflow import CallController, explicit_acceptance

NOW = datetime.fromisoformat("2026-09-19T09:00:00+02:00")


def decide(*ops, language="en"):
    return TurnDecision(language=language, operations=[Operation(**op) for op in ops])


class IncrementalWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = RunStore()
        self.addCleanup(self.store.close)
        self.state = CallState(call_id="incremental", reference_time=NOW)
        self.clinic = FixtureClinic()
        self.controller = CallController(self.state, self.clinic, self.store, Dispatcher(self.store, "simulation"))

    async def turn(self, text, *ops, present=True, language="en"):
        reply = await self.controller.turn(text, decide(*ops, language=language))
        if present:
            self.controller.presented(reply)
        return reply

    async def create(self, action="book", intent="one", subject="Lina Demo"):
        await self.turn(subject, {"op": "create", "intent_id": intent, "subject": subject,
                                  "action": action, "evidence": subject})

    async def identify(self, intent="one", name="Lina Demo", born="1990-01-01"):
        await self.turn(name, {"op": "identify", "intent_id": intent, "evidence": name,
                               "identity": {"name": name, "date_of_birth": born}})

    async def offer(self, action="book", intent="one"):
        await self.create(action, intent)
        await self.identify(intent)
        return await self.turn("tomorrow", {"op": "prepare", "intent_id": intent, "when": "tomorrow",
                                            "specialty_id": "general_practice", "evidence": "tomorrow"})

    async def confirm(self, intent="one", text="Yes, option one", revision=None):
        return await self.turn(text, {"op": "confirm", "intent_id": intent, "option": 1,
                                     "offer_revision": revision or self.state.intents[intent].revision,
                                     "evidence": text})

    async def test_partial_identity_is_merged_without_reasking_name(self):
        await self.create()
        first = await self.turn("Lina Demo", {"op": "identify", "intent_id": "one", "evidence": "Lina Demo",
                                             "identity": {"name": "Lina Demo"}})
        self.assertIn("date of birth", first.text.lower())
        self.assertNotIn("full name", first.text.lower())
        await self.turn("1990-01-01", {"op": "identify", "intent_id": "one", "evidence": "1990-01-01",
                                     "identity": {"date_of_birth": "1990-01-01"}})
        self.assertEqual(self.state.intents["one"].patient["patient_id"], "fixture-adult")

    async def test_identity_completion_resumes_the_collected_request(self):
        await self.create()
        await self.turn("Thursday Norte", {"op": "prepare", "intent_id": "one", "when": "Thursday",
            "location_id": "norte", "specialty_id": "general_practice", "evidence": "Thursday"})
        self.assertFalse(self.state.intents["one"].offers)
        await self.identify()
        self.assertTrue(self.state.intents["one"].offers)
        self.assertEqual(self.state.intents["one"].criteria.when, "Thursday")
        self.assertEqual(self.state.intents["one"].status, "awaiting_confirmation")

    async def test_registration_identity_fields_do_not_lookup_an_existing_patient(self):
        await self.create("register")
        self.clinic.identify = AsyncMock()
        await self.turn("1990-01-01", {"op": "identify", "intent_id": "one", "identity": {"date_of_birth": "1990-01-01"},
                                     "evidence": "1990-01-01"})
        self.assertEqual(self.state.intents["one"].registration_fields.date_of_birth, "1990-01-01")
        self.assertNotIn("date_of_birth", self.state.intents["one"].missing_fields)
        self.clinic.identify.assert_not_called()

    async def test_corrections_carry_other_constraints(self):
        await self.create()
        await self.identify()
        await self.turn("Thursday at Norte with Doctor Demo", {"op": "prepare", "intent_id": "one",
            "specialty_id": "general_practice", "when": "Thursday", "location_id": "norte",
            "doctor_name": "Doctor Demo", "evidence": "Thursday"})
        original = self.clinic.prepare
        self.clinic.prepare = AsyncMock(side_effect=original)
        await self.turn("Friday instead", {"op": "prepare", "intent_id": "one", "when": "Friday",
                                           "evidence": "Friday"})
        merged = self.clinic.prepare.call_args.args[2]
        self.assertEqual(merged.location_id, "norte")
        self.assertEqual(merged.doctor_name, "Doctor Demo")
        self.assertEqual(merged.specialty_id, "general_practice")
        self.assertEqual(merged.when, "Friday")

    async def test_time_of_day_correction_preserves_date_provider_and_site(self):
        await self.create()
        await self.identify()
        await self.turn("Thursday morning at Norte", {"op": "prepare", "intent_id": "one", "when": "Thursday morning",
            "doctor_name": "Doctor Demo", "location_id": "norte", "evidence": "Thursday morning"})
        await self.turn("afternoon instead", {"op": "prepare", "intent_id": "one", "when": "afternoon",
                                            "evidence": "afternoon"})
        criteria = self.state.intents["one"].criteria
        self.assertEqual(criteria.when, "2026-09-24")
        self.assertEqual(criteria.part_of_day, "afternoon")
        self.assertEqual(criteria.location_id, "norte")
        self.assertEqual(criteria.doctor_name, "Doctor Demo")

    async def test_clock_correction_preserves_date_and_date_correction_preserves_clock(self):
        await self.create()
        await self.identify()
        await self.turn("Thursday at 10:00", {"op": "prepare", "intent_id": "one", "when": "Thursday at 10:00", "evidence": "Thursday"})
        await self.turn("10:30 instead", {"op": "prepare", "intent_id": "one", "when": "10:30", "evidence": "10:30"})
        self.assertEqual(self.state.intents["one"].criteria.when, "2026-09-24")
        self.assertEqual(self.state.intents["one"].criteria.time_of_day, "10:30")
        await self.turn("Friday", {"op": "prepare", "intent_id": "one", "when": "Friday", "evidence": "Friday"})
        self.assertEqual(self.state.intents["one"].criteria.time_of_day, "10:30")

    async def test_supplemental_insurance_must_be_named_by_the_caller(self):
        await self.offer()
        await self.turn("I have insurance", {"op": "prepare", "intent_id": "one",
            "criteria": {"insurers": ["Premium"]}, "evidence": "insurance"})
        self.assertFalse(self.state.intents["one"].offers)
        self.assertEqual(self.state.intents["one"].validation_errors, {"insurers": "caller_must_name_plan"})
        self.assertFalse(self.state.intents["one"].criteria.insurers)

    async def test_explicit_constraint_clear_does_not_clear_other_fields(self):
        await self.create()
        await self.identify()
        await self.turn("Thursday Norte", {"op": "prepare", "intent_id": "one", "when": "Thursday",
            "location_id": "norte", "evidence": "Thursday"})
        await self.turn("any site", {"op": "prepare", "intent_id": "one", "clear_fields": ["location_id"], "evidence": "any site"})
        self.assertEqual(self.state.intents["one"].criteria.when, "Thursday")
        self.assertIsNone(self.state.intents["one"].criteria.location_id)

    async def test_registration_collects_each_field_across_turns(self):
        await self.create("register")
        fields = {"given_name": "Lina", "first_surname": "Demo", "second_surname": "Test",
                  "national_id": "12345678Z", "date_of_birth": "1990-01-01", "phone": "612345678",
                  "email": "lina@example.test", "insurer": "sanitas"}
        for key, value in fields.items():
            await self.turn(value, {"op": "prepare", "intent_id": "one", "evidence": value,
                                   "registration_fields": {key: value}})
        intent = self.state.intents["one"]
        self.assertEqual(intent.status, "awaiting_confirmation")
        self.assertFalse(intent.missing_fields)
        await self.confirm()
        self.assertEqual(intent.receipts[0]["action"], "register")
        self.assertEqual(intent.receipts[0]["payload"]["national_id"], "12345678Z")

    async def test_registration_invalid_id_never_infers_confirmation_letter(self):
        await self.create("register")
        for national_id in ("12345678", "12345678A", "12345678Zextra"):
            reply = await self.turn(national_id, {"op": "prepare", "intent_id": "one", "evidence": national_id,
                                                "registration_fields": {"national_id": national_id}})
            self.assertFalse(self.state.intents["one"].offers)
            self.assertIn("national_id", self.state.intents["one"].validation_errors)
            self.assertIn("letter", reply.text.lower())

    async def test_booking_cancel_and_reschedule_complete_only_after_confirmation(self):
        for action in ("book", "cancel", "reschedule"):
            with self.subTest(action=action):
                await self.offer(action, action)
                self.assertFalse(self.state.intents[action].receipts)
                await self.confirm(action)
                self.assertEqual(self.state.intents[action].receipts[0]["action"], action)
                self.assertEqual(self.state.intents[action].status, "completed")

    async def test_missing_identity_is_not_a_patient_not_found_refusal(self):
        await self.create()
        await self.turn("tomorrow", {"op": "prepare", "intent_id": "one", "when": "tomorrow", "evidence": "tomorrow"})
        self.assertIsNone(self.state.intents["one"].blocking_reason)
        await self.turn("no", {"op": "refuse", "intent_id": "one", "reason": "patient_not_found", "evidence": "no"})
        self.assertFalse(self.state.intents["one"].receipts)

    async def test_no_action_retains_actual_reason(self):
        await self.create()
        await self.identify()
        reply = await self.turn("fully booked", {"op": "prepare", "intent_id": "one", "when": "fully booked",
                                                "specialty_id": "general_practice", "evidence": "fully booked"})
        self.assertIn("availability", reply.text.lower())
        await self.turn("No other date", {"op": "refuse", "intent_id": "one", "evidence": "No other date"})
        self.assertEqual(self.state.intents["one"].receipts[0]["payload"]["reason"], "no_availability")

    async def test_refusal_cannot_preempt_a_caller_requesting_an_alternative(self):
        await self.create()
        await self.identify()
        await self.turn("fully booked", {"op": "prepare", "intent_id": "one", "when": "fully booked",
                                         "specialty_id": "general_practice", "evidence": "fully booked"})
        await self.turn("Try Friday", {"op": "refuse", "intent_id": "one", "evidence": "Try Friday"})
        self.assertFalse(self.state.intents["one"].receipts)
        self.assertFalse(self.state.all_resolved)

    async def test_out_of_scope_and_escalation_do_not_book(self):
        await self.create("no_action")
        await self.turn("I need tax advice", {"op": "refuse", "intent_id": "one", "reason": "out_of_scope",
                                            "evidence": "tax advice"})
        self.assertEqual(self.state.intents["one"].receipts[0]["action"], "no_action")
        await self.turn("I have chest pain")
        self.assertEqual(self.state.intents["urgent"].receipts[0]["action"], "escalate")

    async def test_mixed_correction_and_confirmation_rejects_before_write(self):
        await self.offer()
        revision = self.state.intents["one"].revision
        await self.turn("Yes, Friday", {"op": "confirm", "intent_id": "one", "option": 1,
                                       "offer_revision": revision, "evidence": "Yes"},
                        {"op": "prepare", "intent_id": "one", "when": "Friday", "evidence": "Friday"})
        self.assertFalse(self.state.intents["one"].receipts)
        self.assertEqual(self.state.intents["one"].criteria.when, "Friday")
        self.assertGreater(self.state.intents["one"].revision, revision)
        await self.confirm(revision=revision)
        self.assertFalse(self.state.intents["one"].receipts)

    async def test_yes_cannot_target_an_older_patient_offer(self):
        await self.offer(intent="older")
        await self.offer(intent="latest")
        await self.confirm(intent="older")
        self.assertFalse(self.state.intents["older"].receipts)
        await self.confirm(intent="latest")
        self.assertEqual(self.state.intents["latest"].status, "completed")

    async def test_collective_confirmation_validates_every_revision_before_writing(self):
        await self.offer(intent="first")
        await self.offer(intent="second")
        await self.turn("Monday for both", {"op": "prepare", "intent_id": "first", "when": "Monday", "evidence": "Monday"},
                        {"op": "prepare", "intent_id": "second", "when": "Monday", "evidence": "Monday"})
        await self.turn("Yes both option one", {"op": "confirm", "intent_id": "first", "option": 1,
            "offer_revision": 2, "evidence": "Yes"}, {"op": "confirm", "intent_id": "second", "option": 1,
            "offer_revision": 1, "evidence": "Yes"})
        self.assertFalse(self.state.intents["first"].receipts)
        self.assertFalse(self.state.intents["second"].receipts)

    async def test_option_must_agree_with_spoken_selection(self):
        await self.offer()
        await self.confirm(text="Yes, option two")
        self.assertFalse(self.state.intents["one"].receipts)

    async def test_identity_change_does_not_reuse_previous_second_factor(self):
        await self.offer()
        await self.turn("Roc Demo instead", {"op": "identify", "intent_id": "one", "evidence": "Roc Demo",
                                           "identity": {"name": "Roc Demo"}})
        intent = self.state.intents["one"]
        self.assertIsNone(intent.patient)
        self.assertNotIn("date_of_birth", intent.identity_inputs)
        self.assertFalse(intent.offers)

    async def test_finish_cannot_precede_a_new_unresolved_request(self):
        await self.offer()
        await self.confirm()
        reply = await self.turn("Also my child", {"op": "finish"}, {"op": "create", "intent_id": "child",
            "subject": "child", "action": "book", "evidence": "my child"})
        self.assertFalse(reply.completion)
        self.assertNotIn("Goodbye", reply.text)

    async def test_failed_submission_blocks_reconfirmation_and_never_claims_success(self):
        await self.offer()
        self.controller.dispatcher.execute = AsyncMock(return_value={"accepted": False})
        reply = await self.confirm()
        self.assertNotIn("recorded", reply.text.lower())
        await self.confirm()
        self.assertEqual(self.controller.dispatcher.execute.await_count, 1)

    async def test_interrupted_replay_is_not_a_presented_offer(self):
        await self.offer()
        intent = self.state.intents["one"]
        revision = intent.revision
        self.controller.interrupt()
        await self.confirm(revision=revision)
        self.assertFalse(intent.receipts)

    async def test_normal_caller_input_keeps_a_fully_presented_offer_confirmable(self):
        await self.offer()
        self.controller.interrupt(source="caller_input")
        await self.confirm()
        self.assertEqual(self.state.intents["one"].status, "completed")

    async def test_nonmedical_escalation_needs_a_supported_reason(self):
        await self.create("escalate")
        await self.turn("Please help", {"op": "escalate", "intent_id": "one", "reason": "invented", "evidence": "help"})
        self.assertFalse(self.state.intents["one"].receipts)
        await self.turn("I need a service you do not provide", {"op": "escalate", "intent_id": "one",
            "reason": "out_of_scope", "evidence": "a service you do not provide"})
        self.assertEqual(self.state.intents["one"].receipts[0]["action"], "escalate")

    async def test_unknown_submission_is_not_retried_with_a_different_offer(self):
        await self.offer()
        self.controller.dispatcher.execute = AsyncMock(side_effect=TimeoutError())
        await self.confirm()
        self.assertTrue(self.state.intents["one"].submission_uncertain)
        await self.turn("Friday", {"op": "prepare", "intent_id": "one", "when": "Friday", "evidence": "Friday"})
        self.assertFalse(self.state.intents["one"].offers)
        self.assertEqual(self.controller.dispatcher.execute.await_count, 1)
        self.assertFalse(self.state.all_resolved)

    async def test_malformed_appointment_id_cannot_reuse_old_offer(self):
        await self.offer("cancel")
        await self.turn("../other", {"op": "prepare", "intent_id": "one", "appointment_id": "../other", "evidence": "../other"})
        self.assertFalse(self.state.intents["one"].offers)
        await self.confirm()
        self.assertFalse(self.state.intents["one"].receipts)

    async def test_actionable_missing_fields_in_all_languages(self):
        for language, word in (("en", "date of birth"), ("es", "nacimiento"), ("ca", "naixement")):
            self.state.language = language
            intent = language
            await self.create(intent=intent)
            reply = await self.turn("Lina Demo", {"op": "identify", "intent_id": intent, "evidence": "Lina Demo",
                                                 "identity": {"name": "Lina Demo"}}, language=language)
            self.assertIn(word, reply.text.lower())


class ConsentGrammarTests(unittest.TestCase):
    def test_questions_conditional_and_new_requests_are_not_acceptance(self):
        for text in ("Yes?", "Yes if it is free", "Okay maybe", "Yes, cancel my other appointment too",
                     "Sí, pero el viernes", "Sí si es gratis", "D'acord, però demà", "yes not sure", "yes tomorrow"):
            with self.subTest(text=text):
                self.assertFalse(explicit_acceptance(text))

    def test_plain_acceptance_in_all_languages(self):
        for text in ("Yes", "Yes, option one", "Sí, la segunda opción", "D'acord", "Confirmo la opción 1"):
            with self.subTest(text=text):
                self.assertTrue(explicit_acceptance(text))
