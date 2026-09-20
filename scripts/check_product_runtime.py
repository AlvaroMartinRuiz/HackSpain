"""Offline API, durable trace and metrics checks. No external providers."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import unittest

os.environ["DB_PATH"] = ":memory:"
os.environ["TTS_WARM_CACHE"] = "false"
os.environ["SMART_TURN"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from src.main import app
from src.obs.store import CallStore


class ProductRuntimeChecks(unittest.TestCase):
    def test_dashboard_and_demo_routes(self):
        with TestClient(app, client=("127.0.0.1", 12345)) as http:
            for path in ("/", "/ops", "/demo", "/health", "/api/console/demo/story",
                         "/api/console/reliability", "/static/ops.css", "/static/ops.js"):
                with self.subTest(path=path):
                    self.assertEqual(http.get(path).status_code, 200)
            story = http.get("/api/console/demo/story").json()
            self.assertTrue(story["product"]["resolution"]["outcomes"][0]["dry_run"])
            self.assertFalse(story["product"]["resolution"]["outcomes"][0]["accepted"])

    def test_demo_remains_protected_remotely(self):
        with TestClient(app, client=("203.0.113.20", 12345)) as http:
            self.assertEqual(http.get("/demo").status_code, 401)
            self.assertEqual(http.get("/health").status_code, 200)

    def test_counts_records_not_calls_or_simulations(self):
        store = CallStore(":memory:")
        try:
            call = store.open_call("metrics", None)
            call.submissions = [
                {"action": "book", "accepted": True, "dry_run": True},
                {"action": "cancel", "accepted": False},
                {"action": "book", "accepted": True},
                {"action": "cancel", "accepted": True},
            ]
            self.assertEqual(store.aggregate()["with_accepted_submission"], 2)
            store.close_call("metrics")
            self.assertEqual(store.aggregate()["with_accepted_submission"], 2)
        finally:
            store._db.close()

    def test_chart_survives_reload(self):
        store = CallStore(":memory:")
        try:
            store.open_call("replay", None)
            chart = {"patient": {"patient_id": "P-test"}, "visit_count": 11,
                     "last_visit": {"provider_name": "Test clinician"}, "upcoming": []}
            asyncio.run(store.record("replay", "tool_call", {"name": "open_chart", "result": chart}))
            store.close_call("replay")
            store._finished.clear()
            restored = store.load("replay")
            self.assertEqual(restored.chart["visit_count"], 11)
            self.assertEqual(restored.tool_calls[0]["result"], chart)
        finally:
            store._db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
