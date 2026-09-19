from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock

from v2.domain.catalog import Catalog
from v2.clinic import ClinicBoundary, Dispatcher
from v2.models import CallState, Identity, Intent, Operation, SchedulingCriteria
from v2.store import RunStore
from v2.workflow import CallController
from v2.tests.test_domain_workflows import decide

NOW = datetime.fromisoformat("2026-09-19T09:00:00+02:00")
PATIENT = {"patient_id": "patient-one", "given_name": "Ada", "first_surname": "Sample", "second_surname": "Test",
           "date_of_birth": "1990-01-01", "national_id": "12345678Z", "phone": "612345678", "insurer": "basic",
           "has_visited_before": False, "referrals": []}


def catalog():
    return Catalog({"calendar": {"starts": "2026-09-19", "ends": "2026-12-31", "max_span_days": 14},
        "locations": [{"id": key, "name": name, "address": name, "latitude": 0, "longitude": 0,
                       "hours": [{"weekday": day, "intervals": ["08:00-20:00"]}
                                 for day in ("monday", "tuesday", "wednesday", "thursday", "friday")]}
                      for key, name in (("centro", "Centro"), ("norte", "Norte"))],
        "providers": [{"id": "doctor-one", "name": "Alba Serra", "specialty_id": "general_practice",
                       "languages": ["en", "es", "ca"], "schedules": [{"location_id": "centro"}, {"location_id": "norte"}]},
                      {"id": "doctor-two", "name": "Biel Costa", "specialty_id": "general_practice",
                       "languages": ["en", "es"], "schedules": [{"location_id": "centro"}, {"location_id": "norte"}]}]
                     + [{"id": f"doctor-{specialty}", "name": specialty.title() + " Doctor", "specialty_id": specialty,
                         "languages": ["en", "es", "ca"], "schedules": [{"location_id": "centro"}]}
                        for specialty in ("dermatology", "physiotherapy", "paediatrics")],
        "specialties": [{"id": "general_practice", "name": "General", "min_age_months": 180},
                        {"id": "paediatrics", "name": "Paediatrics", "min_age_months": 0, "max_age_months": 179},
                        {"id": "physiotherapy", "name": "Physiotherapy", "min_age_months": 0, "referral_required": True},
                        {"id": "dermatology", "name": "Dermatology", "min_age_months": 0,
                         "not_covered_by": [{"id": "basic"}]}],
        "plans": [{"id": "basic", "name": "Basic"}, {"id": "premium", "name": "Premium"}],
        "appointment_types": [{"id": "first_visit", "name": "First visit", "duration_minutes": 15,
                               "new_patient_requirement": "new_only"}]})


def slot(when="2026-09-21T10:00:00+02:00", doctor="doctor-one", site="centro", specialty="general_practice", plans=None):
    name = "Alba Serra" if doctor == "doctor-one" else "Biel Costa"
    if specialty != "general_practice":
        doctor, name = f"doctor-{specialty}", specialty.title() + " Doctor"
    return {"provider_id": doctor, "provider_name": name,
            "specialty_id": specialty, "location_id": site, "appointment_type_id": "first_visit",
            "start_time": when, "duration_minutes": 15, "payable_with": ["basic"] if plans is None else plans}


def appointment(key="appointment-one", when="2026-09-21 09:00", doctor="Alba Serra", site="centro"):
    return {"appointment_id": key, "provider_id": "doctor-one" if doctor == "Alba Serra" else "doctor-two",
            "provider_name": doctor, "location_id": site, "location_name": site.title(),
            "specialty_id": "general_practice", "when": when, "slot": when.replace(" ", "T") + ":00+02:00"}


class ClinicRuleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = AsyncMock()
        self.client.directory.return_value = [deepcopy(PATIENT)]
        self.client.appointments.return_value = []
        self.client.availability.return_value = {"slots": [slot()], "blocked": []}
        self.boundary = ClinicBoundary(self.client, catalog())
        self.state = CallState(call_id="clinic-rules", reference_time=NOW)
        self.intent = Intent(intent_id="one", action="book", subject="Ada Sample Test", revision=1,
                             identity_status="verified", patient={**deepcopy(PATIENT), "upcoming": []})

    async def prepare(self, **kwargs):
        return await self.boundary.prepare(self.state, self.intent, Operation(op="prepare", **kwargs))

    async def test_cancel_ambiguous_doctor_requires_date_not_first_appointment(self):
        self.intent.action = "cancel"
        self.intent.patient["upcoming"] = [appointment(), appointment("appointment-two", "2026-09-22 09:00")]
        offers, reason = await self.prepare(doctor_name="Serra")
        self.assertFalse(offers)
        self.assertIsNone(reason)
        self.assertEqual(self.intent.missing_fields, ["appointment_id"])
        self.assertEqual(len(self.intent.choices), 2)
        offers, _ = await self.prepare(doctor_name="Serra", when="2026-09-22")
        self.assertEqual(offers[0].payload, {"appointment_id": "appointment-two"})

    async def test_cancel_same_doctor_same_day_requires_time(self):
        self.intent.action = "cancel"
        self.intent.patient["upcoming"] = [appointment(), appointment("appointment-two", "2026-09-21 10:30")]
        offers, _ = await self.prepare(doctor_name="Serra", when="Monday")
        self.assertFalse(offers)
        offers, _ = await self.prepare(doctor_name="Serra", when="Monday at 10:30")
        self.assertEqual(offers[0].payload["appointment_id"], "appointment-two")

    async def test_requested_clock_is_never_silently_dropped(self):
        self.client.availability.return_value = {"slots": [slot(), slot("2026-09-21T10:30:00+02:00")], "blocked": []}
        offers, _ = await self.prepare(specialty_id="general_practice", when="Monday at 10:30")
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].display["when"], "2026-09-21 10:30")
        self.assertFalse(offers[0].display["alternative"])
        offers, _ = await self.prepare(specialty_id="general_practice", when="Monday at 11:30")
        self.assertTrue(all(offer.display["alternative"] for offer in offers))

    async def test_cancel_selection_obeys_all_constraints_even_with_id(self):
        self.intent.action = "cancel"
        self.intent.patient["upcoming"] = [appointment()]
        offers, reason = await self.prepare(appointment_id="appointment-one", location_id="norte")
        self.assertFalse(offers)
        self.assertIsNone(reason)
        self.assertIn("appointment_id", self.intent.validation_errors)

    async def test_cancel_foreign_id_does_not_fall_back_to_single_appointment(self):
        self.intent.action = "cancel"
        self.intent.patient["upcoming"] = [appointment()]
        offers, reason = await self.prepare(appointment_id="foreign")
        self.assertFalse(offers)
        self.assertIsNone(reason)

    async def test_reschedule_preserves_original_and_destination_separately(self):
        self.intent.action = "reschedule"
        self.intent.patient["upcoming"] = [appointment(), appointment("appointment-two", doctor="Biel Costa")]
        self.client.availability.return_value = {"slots": [slot("2026-09-22T10:00:00+02:00", "doctor-two", "norte")], "blocked": []}
        offers, _ = await self.prepare(when="2026-09-22", doctor_name="Biel Costa", location_id="norte",
                                      criteria=SchedulingCriteria(appointment_doctor_name="Alba Serra"))
        self.assertEqual(offers[0].payload["appointment_id"], "appointment-one")
        self.assertEqual(offers[0].payload["provider_id"], "doctor-two")
        self.assertEqual(offers[0].display["previous"]["doctor"], "Alba Serra")

    async def test_reschedule_does_not_treat_new_date_as_existing_selection(self):
        self.intent.action = "reschedule"
        self.intent.patient["upcoming"] = [appointment(), appointment("appointment-two", "2026-09-22 09:00")]
        offers, _ = await self.prepare(when="2026-09-22")
        self.assertFalse(offers)
        self.assertEqual(self.intent.missing_fields, ["appointment_id"])
        self.client.availability.assert_not_called()

    async def test_second_plan_can_cover_specialty_but_is_not_invented(self):
        self.client.availability.return_value = {"slots": [slot(specialty="dermatology", plans=["premium"])], "blocked": []}
        offers, reason = await self.prepare(specialty_id="dermatology")
        self.assertFalse(offers)
        self.assertEqual(reason, "specialty_not_covered")
        offers, reason = await self.prepare(specialty_id="dermatology", criteria=SchedulingCriteria(insurers=["Premium"]))
        self.assertIsNone(reason)
        self.assertEqual(offers[0].payload["policy_id"], "premium")
        self.assertEqual(self.intent.patient["insurer"], "basic")
        self.assertEqual(self.client.availability.call_args.kwargs["insurer"], ["basic", "premium"])

    async def test_single_payable_plan_not_held_is_never_inferred(self):
        self.client.availability.return_value = {"slots": [slot(plans=["premium"])], "blocked": []}
        offers, reason = await self.prepare(specialty_id="general_practice")
        self.assertFalse(offers)
        self.assertEqual(reason, "specialty_not_covered")

    async def test_unknown_plan_requires_clarification_not_no_action(self):
        offers, reason = await self.prepare(specialty_id="general_practice", criteria=SchedulingCriteria(insurers=["Unrecognized"] ))
        self.assertFalse(offers)
        self.assertIsNone(reason)
        self.assertEqual(self.intent.validation_errors, {"insurers": "unknown_plan"})
        self.client.availability.assert_not_called()

    async def test_referral_must_come_from_clinic_not_the_caller(self):
        offers, reason = await self.prepare(specialty_id="physiotherapy", criteria=SchedulingCriteria(insurers=["Premium"]))
        self.assertFalse(offers)
        self.assertEqual(reason, "referral_required")
        self.client.availability.assert_not_called()
        self.intent.patient["referrals"] = ["physiotherapy"]
        self.client.availability.return_value = {"slots": [slot(specialty="physiotherapy")], "blocked": []}
        offers, reason = await self.prepare(specialty_id="physiotherapy")
        self.assertIsNone(reason)
        self.assertTrue(offers)

    async def test_closed_day_alternative_is_explicit(self):
        offers, _ = await self.prepare(specialty_id="general_practice", when="tomorrow")
        self.assertTrue(offers[0].display["alternative"])
        self.assertEqual(offers[0].display["alternative_reason"], "clinic_closed")
        store = RunStore()
        self.addCleanup(store.close)
        controller = CallController(self.state, self.boundary, store, Dispatcher(store, "simulation"))
        for language, word in (("en", "Alternative"), ("es", "Alternativa"), ("ca", "Alternativa")):
            self.state.language = language
            self.assertIn(word, controller._describe_offer(self.intent, offers[0], 1))

    async def test_each_wrong_time_alternative_is_marked(self):
        self.client.availability.return_value = {"slots": [slot(), slot("2026-09-22T16:00:00+02:00")], "blocked": []}
        offers, _ = await self.prepare(specialty_id="general_practice", when="Monday afternoon")
        self.assertEqual(len(offers), 2)
        self.assertTrue(all(offer.display["alternative"] for offer in offers))

    async def test_catalan_relative_date_and_time_are_resolved(self):
        self.client.availability.return_value = {"slots": [slot("2026-09-21T16:00:00+02:00")], "blocked": []}
        offers, _ = await self.prepare(specialty_id="general_practice", when="demà passat a la tarda")
        self.assertFalse(offers[0].display["alternative"])
        self.assertEqual(self.client.availability.call_args.kwargs["date_from"], "2026-09-21")

    async def test_unparsed_date_is_not_silently_earliest(self):
        offers, reason = await self.prepare(specialty_id="general_practice", when="some special festival")
        self.assertFalse(offers)
        self.assertIsNone(reason)
        self.assertEqual(self.intent.validation_errors, {"when": "unrecognized_date"})
        self.client.availability.assert_not_called()

    async def test_ranges_are_clarified_instead_of_silently_ignoring_an_endpoint(self):
        for phrase in ("Monday before 10:30", "between Monday and Friday", "entre el lunes y el viernes", "abans de les 10:30"):
            offers, reason = await self.prepare(specialty_id="general_practice", when=phrase)
            self.assertFalse(offers)
            self.assertIsNone(reason)
            self.assertEqual(self.intent.validation_errors, {"when": "unsupported_window"})
        self.client.availability.assert_not_called()

    async def test_iso_date_preserves_the_requested_day_in_the_backend_query(self):
        self.client.availability.return_value = {"slots": [slot("2026-09-25T10:00:00+02:00")], "blocked": []}
        offers, _ = await self.prepare(specialty_id="general_practice", when="2026-09-25")
        self.assertEqual(self.client.availability.call_args.kwargs["date_from"], "2026-09-25")
        self.assertFalse(offers[0].display["alternative"])

    async def test_backend_cannot_drop_provider_or_site_constraints(self):
        self.client.availability.return_value = {"slots": [slot(doctor="doctor-two", site="norte")], "blocked": []}
        offers, reason = await self.prepare(doctor_name="Alba Serra", location_id="centro")
        self.assertFalse(offers)
        self.assertEqual(reason, "no_availability")

    async def test_clinic_restriction_is_preserved(self):
        for reason in ("insurer_referral_required", "allowance_exhausted", "provider_not_in_network", "location_hours"):
            self.client.availability.return_value = {"slots": [], "blocked": [{"restriction": reason}]}
            offers, actual = await self.prepare(specialty_id="general_practice")
            self.assertFalse(offers)
            self.assertEqual(actual, reason)

    async def test_availability_read_failure_is_not_a_no_availability_outcome(self):
        self.client.availability.side_effect = TimeoutError()
        with self.assertRaises(RuntimeError):
            await self.prepare(specialty_id="general_practice")

    async def test_unknown_restriction_is_not_reported_as_calendar_full(self):
        self.client.availability.return_value = {"slots": [], "blocked": [{"restriction": "unknown_backend_rule"}]}
        with self.assertRaises(RuntimeError):
            await self.prepare(specialty_id="general_practice")

    async def test_directory_identifiers_are_compared_in_normalized_form(self):
        self.client.directory.return_value = [{**PATIENT, "phone": "+34 612 345 678", "national_id": "12345678-Z"}]
        result = await self.boundary.identify_result(Identity(name="Ada Sample", phone="612345678",
                                                             national_id="12345678Z"), NOW.date())
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["patient"]["patient_id"], PATIENT["patient_id"])

    async def test_bad_identity_letter_never_searches_without_the_id(self):
        result = await self.boundary.identify_result(Identity(name="Ada Sample", national_id="12345678A"), NOW.date())
        self.assertIsNone(result["patient"])
        self.assertEqual(result["status"], "invalid")
        self.client.directory.assert_not_called()

    async def test_valid_id_no_match_does_not_retry_using_softer_fields(self):
        self.client.directory.return_value = []
        result = await self.boundary.identify_result(Identity(name="Ada Sample", national_id="12345678Z"), NOW.date())
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(self.client.directory.await_count, 1)

    async def test_identity_ambiguity_does_not_disclose_patient_candidates(self):
        self.client.directory.return_value = [deepcopy(PATIENT), {**PATIENT, "patient_id": "patient-two"}]
        result = await self.boundary.identify_result(Identity(name="Ada Sample", date_of_birth="1990-01-01"), NOW.date())
        self.assertEqual(result["status"], "ambiguous")
        self.assertNotIn("matches", result)
        self.assertEqual(result["missing_fields"], ["national_id"])

    async def test_malformed_backend_references_never_dispatch(self):
        store = RunStore()
        self.addCleanup(store.close)
        sender = AsyncMock()
        dispatcher = Dispatcher(store, "simulation", sender)
        for value in ("", "../other", "one two", "<script>"):
            with self.assertRaises(ValueError):
                await dispatcher.execute(self.state, self.intent, "cancel", {"appointment_id": value})
        sender.assert_not_called()


class IndependentCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_patients_with_same_doctor_are_independent(self):
        store = RunStore()
        self.addCleanup(store.close)
        state = CallState(call_id="two-cancellations", reference_time=NOW)
        boundary = ClinicBoundary(AsyncMock(), catalog())
        controller = CallController(state, boundary, store, Dispatcher(store, "simulation"))
        for index in (1, 2):
            key = f"patient-{index}"
            state.intents[key] = Intent(intent_id=key, action="cancel", subject=key, identity_status="verified",
                patient={**PATIENT, "patient_id": key, "upcoming": [appointment(f"appointment-{index}")]})
            reply = await controller.turn("Cancel my appointment", decide({"op": "prepare", "intent_id": key,
                "evidence": "my appointment"}))
            controller.presented(reply)
            await controller.turn("Yes", decide({"op": "confirm", "intent_id": key, "option": 1,
                "offer_revision": 1, "evidence": "Yes"}))
        self.assertTrue(state.all_resolved)
        for index in (1, 2):
            self.assertEqual(state.intents[f"patient-{index}"].receipts[0]["payload"]["appointment_id"], f"appointment-{index}")
