from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from v2.models import CallState
from v2.store import RunStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / "runs.db")
        self.store = RunStore(self.path)
        self.addCleanup(self.store.close)
        self.state = CallState(call_id="synthetic", reference_time=datetime(2026, 9, 19, tzinfo=timezone.utc))
        self.store.save(self.state, {"fixture": "synthetic-v1"})

    def test_reopen_retains_versions_events_manifest_and_actions(self):
        key, previous = self.store.begin_action(self.state.run_id, "mine", "cancel", {"appointment_id": "synthetic"})
        self.assertIsNone(previous)
        self.store.event(self.state.run_id, "call_started", {"status": "active"})
        self.state.turn = 1
        self.store.save(self.state)
        other = RunStore(self.path)
        self.addCleanup(other.close)
        report = other.report(self.state.run_id)
        self.assertEqual(report["state"]["turn"], 1)
        self.assertEqual(report["manifest"], {"fixture": "synthetic-v1"})
        self.assertEqual(report["status"], "active")
        self.assertEqual(report["actions"][0]["action_key"], key)
        self.assertEqual(report["actions"][0]["status"], "pending")
        self.assertEqual(other.db.execute("SELECT COUNT(*) FROM state_versions").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            other.save(self.state, {"fixture": "replacement"})
        self.assertEqual(other.report(self.state.run_id)["manifest"], report["manifest"])

    def test_old_schema_is_extended_without_rewriting_or_deleting_rows(self):
        path = str(Path(self.folder.name) / "legacy.db")
        with closing(sqlite3.connect(path)) as legacy, legacy:
            legacy.executescript("""
                CREATE TABLE runs(run_id TEXT PRIMARY KEY,call_id TEXT NOT NULL,mode TEXT NOT NULL,
                    created_at TEXT NOT NULL,state TEXT NOT NULL,manifest TEXT NOT NULL);
                CREATE TABLE events(event_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(run_id,sequence));
                CREATE TABLE actions(action_key TEXT PRIMARY KEY,run_id TEXT NOT NULL,intent_id TEXT NOT NULL,status TEXT NOT NULL,receipt TEXT);
                CREATE TABLE budget(reservation_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,service TEXT NOT NULL,reserved INTEGER NOT NULL,actual INTEGER,status TEXT NOT NULL);
            """)
            legacy.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (self.state.run_id, "synthetic", "simulation", "2026-09-19T00:00:00+00:00", self.state.model_dump_json(), '{"old":true}'))
            legacy.execute("INSERT INTO budget VALUES ('old-cost',?,'mock',123,NULL,'reserved')", (self.state.run_id,))
            legacy.execute("INSERT INTO actions VALUES ('old-key',?,'mine','pending',NULL)", (self.state.run_id,))
        upgraded = RunStore(path)
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.report(self.state.run_id)["manifest"], {"old": True})
        self.assertEqual(upgraded.db.execute("SELECT reserved FROM budget WHERE reservation_id='old-cost'").fetchone()[0], 123)
        self.assertEqual(upgraded.report(self.state.run_id)["actions"][0]["action_key"], "old-key")
        self.assertEqual(upgraded.list_runs()["runs"][0]["status"], "unknown")

    def test_concurrent_events_have_contiguous_sequences(self):
        other = RunStore(self.path)
        self.addCleanup(other.close)
        def emit(index):
            return (other if index % 2 else self.store).event(self.state.run_id, "mock", {"index": index})
        with ThreadPoolExecutor(max_workers=8) as pool:
            identifiers = list(pool.map(emit, range(80)))
        self.assertEqual(len(set(identifiers)), 80)
        self.assertEqual([e["sequence"] for e in self.store.report(self.state.run_id)["events"]], list(range(1, 81)))

    def test_concurrent_actions_execute_only_once_and_require_reconciliation(self):
        other = RunStore(self.path)
        self.addCleanup(other.close)
        def begin(index):
            try:
                return (other if index % 2 else self.store).begin_action(self.state.run_id, "mine", "cancel", {"appointment_id": "synthetic"})[0]
            except RuntimeError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            keys = [key for key in pool.map(begin, range(20)) if key]
        self.assertEqual(len(keys), 1)
        key = keys[0]
        self.store.finish_action(key, {"accepted": False})
        with self.assertRaises(RuntimeError):
            self.store.begin_action(self.state.run_id, "other", "cancel", {"appointment_id": "synthetic"})
        with self.assertRaises(ValueError):
            self.store.finish_action(key, {"accepted": True})
        self.store.reconcile_action(key, {"accepted": True}, evidence="mock-provider-receipt-1")
        self.assertEqual(self.store.begin_action(self.state.run_id, "other", "cancel", {"appointment_id": "synthetic"}), (key, {"accepted": True}))
        self.store.finish_action(key, {"accepted": True})
        with self.assertRaises(ValueError):
            self.store.finish_action(key, {"accepted": False})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM action_history WHERE action_key=?", (key,)).fetchone()[0], 3)

    def test_action_rejection_is_cached_not_automatically_retried(self):
        key, _ = self.store.begin_action(self.state.run_id, "mine", "book", {"patient_id": "synthetic"})
        self.store.reconcile_action(key, {"accepted": False}, evidence="mock-confirmed-not-applied")
        self.assertEqual(self.store.begin_action(self.state.run_id, "mine", "book", {"patient_id": "synthetic"})[1], {"accepted": False})
        with self.assertRaises(ValueError):
            self.store.finish_action("unknown", {"accepted": True})
        with self.assertRaises(ValueError):
            self.store.finish_action(key, {"accepted": 1})

    def test_canonical_action_keys_and_receipt_payload_validation(self):
        payload = {"appointment_id": "synthetic", "call_id": "synthetic"}
        key, _ = self.store.begin_action(self.state.run_id, "mine", "cancel", payload)
        with self.assertRaises(ValueError):
            self.store.finish_action(key, {"accepted": True, "payload": {"appointment_id": "wrong"}})
        receipt = {"accepted": True, "action": "cancel", "payload": payload, "action_key": key}
        self.store.finish_action(key, receipt)
        self.assertEqual(self.store.begin_action(self.state.run_id, "other", "cancel", dict(reversed(list(payload.items())))), (key, receipt))

    def test_invalid_data_does_not_mutate_state(self):
        for payload in ([1], {"bad": float("nan")}, {"bad": object()}, {1: "bad"}, {"bad": "x" * 262_145}):
            with self.subTest(payload=type(payload)), self.assertRaises(ValueError):
                self.store.event(self.state.run_id, "bad", payload)
        with self.assertRaises(ValueError):
            self.store.event("missing", "call_started", {"status": "active"})
        with self.assertRaises(ValueError):
            self.store.event(self.state.run_id, "call_ended", {"status": "active"})
        self.assertFalse(self.store.report(self.state.run_id)["events"])

    def test_listing_stable_tie_keysets_and_no_trace_or_grade_inference(self):
        for index in range(6):
            state = self.state.model_copy(update={"run_id": f"run-{index}", "call_id": f"call-{index}", "completion_requested": True})
            self.store.save(state)
            self.store.db.execute("UPDATE runs SET created_at='2026-01-01T00:00:00+00:00' WHERE run_id=?", (state.run_id,))
        first = self.store.list_runs(3)
        self.assertEqual(first["runs"][1]["run_id"], "run-5")
        self.assertIsNone(first["runs"][1]["official_grade"])
        self.assertEqual(first["runs"][1]["status"], "unknown")
        self.assertNotIn("events", first["runs"][0])
        self.assertNotIn("state", first["runs"][0])
        inserted = self.state.model_copy(update={"run_id": "newer", "call_id": "newer"})
        self.store.save(inserted)
        seen = [r["run_id"] for r in first["runs"]]
        cursor = first["next_cursor"]
        while cursor:
            page = self.store.list_runs(3, cursor)
            seen.extend(r["run_id"] for r in page["runs"])
            cursor = page["next_cursor"]
        self.assertEqual(len(seen), 7)
        self.assertEqual(len(set(seen)), 7)
        self.assertNotIn("newer", seen)
        self.assertEqual(self.store.list_runs(3, first["next_cursor"]), self.store.list_runs(3, first["next_cursor"]))

    def test_stale_state_and_identity_changes_cannot_replace_current_state(self):
        old = self.state.model_copy(deep=True)
        self.state.turn = 2
        self.state.revision = 2
        self.store.save(self.state)
        with self.assertRaises(ValueError):
            self.store.save(old)
        changed = self.state.model_copy(update={"call_id": "wrong-call"})
        with self.assertRaises(ValueError):
            self.store.save(changed)
        self.assertEqual(self.store.report(self.state.run_id)["state"]["revision"], 2)

    def test_listing_projection_agrees_with_report_without_reading_event_trace(self):
        from v2.evaluation import metrics
        self.store.begin_action(self.state.run_id, "pending", "cancel", {"appointment_id": "synthetic"})
        self.store.finish_run(self.state.run_id, "error")
        report = self.store.report(self.state.run_id)
        self.assertFalse(report["metrics"]["missing_submission"])
        self.assertEqual(report["metrics"]["unresolved_actions"], 1)
        queries = []
        self.store.db.set_trace_callback(queries.append)
        try:
            summary = self.store.list_runs()["runs"][0]
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual(summary["metrics"], metrics(report))
        self.assertFalse(any("FROM events" in query for query in queries))
        self.assertNotIn("synthetic", json.dumps(summary["metrics"]))

    def test_listing_validation_and_lifecycle_never_masks_failure(self):
        for limit in (True, 0, 101, 1.1, "2"):
            with self.assertRaises(ValueError):
                self.store.list_runs(limit)
        for cursor in ("", "!", "x" * 513, 7, base64.urlsafe_b64encode(json.dumps(["naive", "run"]).encode()).decode().rstrip("=")):
            with self.assertRaises(ValueError):
                self.store.list_runs(before=cursor)
        self.store.event(self.state.run_id, "session_started", {"status": "active"})
        self.assertIsNone(self.store.report(self.state.run_id)["metrics"]["missing_submission"])
        self.store.event(self.state.run_id, "call_ended", {"status": "error"})
        self.store.finish_run(self.state.run_id, "completed")
        self.assertEqual(self.store.list_runs()["runs"][0]["status"], "error")
        self.assertIsNone(self.store.report(self.state.run_id)["official_grade"])


if __name__ == "__main__":
    unittest.main()
