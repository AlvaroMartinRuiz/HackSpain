from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import httpx

from v2.evaluation import evaluate_outcome, failure_review, fixture_grade, metrics
from v2.fixtures.outcomes import LANGUAGES, OUTCOMES, caller_goal, fixture_provenance, outcome_oracle, outcome_request
from v2.store import RunStore


class OutcomeEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_six_outcomes_in_all_languages_are_workflow_executions(self):
        store = RunStore()
        self.addCleanup(store.close)
        with patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("network forbidden")):
            for language in LANGUAGES:
                for outcome in OUTCOMES:
                    with self.subTest(language=language, outcome=outcome):
                        report = await evaluate_outcome(outcome, language, store)
                        self.assertTrue(report["fixture_grade"]["passed"], report["fixture_grade"])
                        self.assertEqual(report["status"], "completed")
                        self.assertEqual(report["state"]["language"], language)
                        self.assertEqual(report["metrics"]["simulated_receipts"], 1)
                        self.assertEqual(report["metrics"]["http_accepted"], 0)
                        self.assertEqual(report["metrics"]["audio_status"], "not_measured")
                        self.assertTrue(any(e["kind"] == "caller_turn" for e in report["events"]))
                        self.assertTrue(any(e["kind"] == "action_receipt" for e in report["events"]))
                        self.assertFalse(any(e["kind"] == "audio_output" for e in report["events"]))
                        self.assertIsNone(report["official_grade"])
                        self.assertEqual(report["failure_review"]["primary_category"], "no_failure_observed")
        self.assertEqual(len(store.list_runs()["runs"]), 18)

    async def test_grading_is_independent_of_fixture_names_and_rejects_wrong_entities(self):
        store = RunStore()
        self.addCleanup(store.close)
        for outcome in OUTCOMES:
            report = await evaluate_outcome(outcome, "en", store)
            copied = deepcopy(report)
            copied["manifest"]["fixture"] = "unrelated-name"
            self.assertTrue(fixture_grade(copied, outcome_oracle(outcome))["passed"])
            receipt = next(e for e in copied["events"] if e["kind"] == "action_receipt")["payload"]
            key = next(k for k in receipt["payload"] if k != "call_id")
            receipt["payload"][key] = "wrong-entity-or-value"
            grade = fixture_grade(copied, outcome_oracle(outcome))
            self.assertFalse(grade["passed"])
            self.assertIn("semantic_mismatch", failure_review(copied, grade)["categories"])

    async def test_no_grade_shortcut_for_empty_rejected_unresolved_or_platform_receipts(self):
        store = RunStore()
        self.addCleanup(store.close)
        report = await evaluate_outcome("book", "en", store)
        for mutation in ("empty", "rejected", "unresolved", "platform", "provider_error"):
            copied = deepcopy(report)
            if mutation == "empty":
                copied["state"]["intents"] = {}
            elif mutation == "unresolved":
                copied["actions"][0]["status"] = "pending"
            elif mutation == "provider_error":
                copied["events"].append({"kind": "provider_error", "payload": {"error_type": "TimeoutError"}})
            else:
                receipt = next(e["payload"] for e in copied["events"] if e["kind"] == "action_receipt")
                receipt["accepted" if mutation == "rejected" else "source"] = False if mutation == "rejected" else "platform_receipt"
            self.assertFalse(fixture_grade(copied, outcome_oracle("book"))["passed"], mutation)
        with self.assertRaises(ValueError):
            fixture_grade(report, [])
        with self.assertRaises(ValueError):
            fixture_grade(report, [{"not": "an oracle"}])

    async def test_duplicate_cumulative_receipts_not_counted_twice_but_extra_action_fails(self):
        store = RunStore()
        self.addCleanup(store.close)
        report = await evaluate_outcome("cancel", "en", store)
        event = deepcopy(next(e for e in report["events"] if e["kind"] == "action_receipt"))
        report["events"].append(event)
        self.assertEqual(metrics(report)["simulated_receipts"], 1)
        self.assertTrue(fixture_grade(report, outcome_oracle("cancel"))["passed"])
        event = deepcopy(event)
        event["payload"]["action_key"] = "different-action"
        report["events"].append(event)
        self.assertFalse(fixture_grade(report, outcome_oracle("cancel"))["passed"])

    async def test_provenance_failure_review_and_grades_survive_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "runs.db")
            store = RunStore(path)
            try:
                report = await evaluate_outcome("register", "ca", store, split="holdout", manifest={"revision": "candidate-test"})
                self.assertEqual(report["manifest"]["dataset_split"], "holdout")
                self.assertEqual(report["manifest"]["revision"], "candidate-test")
                self.assertEqual(len(report["manifest"]["code_sha256"]), 64)
                self.assertEqual(len(report["manifest"]["fixture_sha256"]), 64)
            finally:
                store.close()
            reopened = RunStore(path)
            try:
                persisted = reopened.report(report["run_id"])
                kinds = [e["kind"] for e in persisted["events"]]
                self.assertIn("fixture_grade", kinds)
                self.assertIn("failure_review", kinds)
                self.assertIn("evaluation_summary", kinds)
                self.assertEqual(persisted["manifest"], report["manifest"])
                self.assertIsNone(persisted["official_grade"])
            finally:
                reopened.close()

    def test_caller_goals_do_not_include_oracles_or_scripted_decisions(self):
        for outcome in (*OUTCOMES, "multi_intent"):
            goal = caller_goal(outcome)
            for forbidden in ("fixture-", "appointment_id", "patient_id", "offer_revision", "operations", "expected"):
                self.assertNotIn(forbidden, goal)
        for language in LANGUAGES:
            for outcome in OUTCOMES:
                request = outcome_request(outcome, language)
                self.assertTrue(request.turns)
                self.assertEqual(fixture_provenance(outcome, language), fixture_provenance(outcome, language))
        with self.assertRaises(ValueError):
            outcome_request("unknown")
        with self.assertRaises(ValueError):
            outcome_request("book", "fr")

    def test_audio_metrics_use_only_actual_cumulative_output_and_unknown_latency(self):
        report = {"state": {"intents": {}}, "events": []}
        self.assertIsNone(metrics(report)["all_intents_completed"])
        self.assertIsNone(metrics(report)["missing_submission"])
        report["events"].extend([
            {"kind": "tts_generated", "payload": {"audio_status": "signal_sent"}},
            {"kind": "response_planned", "payload": {"elapsed_ms": 12}},
            {"kind": "response_planned", "payload": {"elapsed_ms": -1}},
            {"kind": "response_planned", "payload": {}},
        ])
        self.assertEqual(metrics(report)["audio_status"], "not_measured")
        self.assertEqual(metrics(report)["response_ms"], [12])
        self.assertIsNone(metrics(report)["meaningful_response_ms"])
        self.assertIsNone(metrics(report)["entity_errors"])
        for count in (1, 2, 3):
            report["events"].append({"kind": "audio_output", "payload": {
                "audio_status": "signal_sent", "frames_sent": count, "non_silent_frames_sent": count}})
        measured = metrics(report)
        self.assertEqual(measured["audio_observation"]["frames_sent"], 3)
        self.assertEqual(measured["audio_status"], "signal_sent")
        report["events"].append({"kind": "audio_output", "payload": {"audio_status": "silent"}})
        self.assertEqual(metrics(report)["audio_status"], "unknown")
        report["events"].append({"kind": "audio_output", "payload": {"audio_status": "silent", "finalized": True}})
        self.assertEqual(metrics(report)["audio_status"], "silent")
        self.assertIn("audio_failure", failure_review(report)["categories"])

    def test_provider_invalid_and_semantic_failures_have_distinct_evidence(self):
        report = {"run_id": "synthetic", "state": {"intents": {}}, "events": [
            {"event_id": "safe-reference", "kind": "provider_error", "sequence": 1,
             "payload": {"error_type": "ValidationError"}},
            {"event_id": "safe-reference-2", "kind": "simulation_ended", "sequence": 2,
             "payload": {"reason": "turn_limit"}},
        ], "actions": [{"status": "unresolved"}]}
        review = failure_review(report)
        self.assertEqual(review["primary_category"], "provider_failure")
        self.assertIn("invalid_data", review["categories"])
        self.assertIn("limit_exhausted", review["categories"])
        self.assertIn("unresolved_action", review["categories"])
        self.assertNotIn("payload", review["evidence"][0])
        self.assertIsNone(review["official_grade"])
        self.assertFalse(review["automatic_changes"])
        self.assertTrue(json.dumps(review))


if __name__ == "__main__":
    unittest.main()
