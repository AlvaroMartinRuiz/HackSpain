from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class BudgetExceeded(RuntimeError):
    pass


class RunStore:
    def __init__(self, path: str = ":memory:", cap_microusd: int = 30_000_000):
        if not 0 <= cap_microusd <= 30_000_000:
            raise ValueError("this project's authorized cap is $30")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.cap = cap_microusd
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, call_id TEXT NOT NULL, mode TEXT NOT NULL,
                created_at TEXT NOT NULL, state TEXT NOT NULL, manifest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                timestamp TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS actions (
                action_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, intent_id TEXT NOT NULL,
                status TEXT NOT NULL, receipt TEXT
            );
            CREATE TABLE IF NOT EXISTS budget (
                reservation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, service TEXT NOT NULL,
                reserved INTEGER NOT NULL, actual INTEGER, status TEXT NOT NULL
            );
        """)

    def close(self):
        self.db.close()

    def save(self, state, manifest: dict | None = None):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET state=excluded.state",
                (state.run_id, state.call_id, state.mode, datetime.now(timezone.utc).isoformat(),
                 state.model_dump_json(), json.dumps(manifest or {})),
            )

    def event(self, run_id: str, kind: str, payload: dict):
        with self.lock, self.db:
            sequence = self.db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?",
                                       (run_id,)).fetchone()[0]
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (
                uuid4().hex, run_id, sequence, datetime.now(timezone.utc).isoformat(), kind,
                json.dumps(payload, ensure_ascii=False, default=str),
            ))

    def report(self, run_id: str) -> dict | None:
        with self.lock:
            run = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                return None
            events = self.db.execute("SELECT * FROM events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
        return {"run_id": run_id, "call_id": run["call_id"], "mode": run["mode"],
                "manifest": json.loads(run["manifest"]), "state": json.loads(run["state"]),
                "official_grade": None,
                "events": [{**dict(row), "payload": json.loads(row["payload"])} for row in events]}

    def begin_action(self, run_id: str, intent_id: str, action: str, payload: dict) -> tuple[str, dict | None]:
        canonical = json.dumps([run_id, action, payload], sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(canonical.encode()).hexdigest()
        with self.lock, self.db:
            row = self.db.execute("SELECT status,receipt FROM actions WHERE action_key=?", (key,)).fetchone()
            if row:
                if row["status"] == "accepted":
                    return key, json.loads(row["receipt"])
                raise RuntimeError("action outcome unresolved; reconcile before retrying")
            self.db.execute("INSERT INTO actions VALUES (?,?,?,?,NULL)", (key, run_id, intent_id, "pending"))
        return key, None

    def finish_action(self, key: str, receipt: dict):
        status = "accepted" if receipt["accepted"] else "unresolved"
        with self.lock, self.db:
            self.db.execute("UPDATE actions SET status=?,receipt=? WHERE action_key=?",
                            (status, json.dumps(receipt), key))

    def reserve(self, run_id: str, service: str, microusd: int) -> str:
        if microusd <= 0:
            raise ValueError("a positive cost reservation is required")
        reservation = uuid4().hex
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                used = self.db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM budget").fetchone()[0]
                if used + microusd > self.cap:
                    raise BudgetExceeded("authorized API budget exhausted")
                self.db.execute("INSERT INTO budget VALUES (?,?,?,?,NULL,?)",
                                (reservation, run_id, service, microusd, "reserved"))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        return reservation

    def settle(self, reservation: str, actual_microusd: int):
        if actual_microusd < 0:
            raise ValueError("cost cannot be negative")
        with self.lock, self.db:
            row = self.db.execute("SELECT status FROM budget WHERE reservation_id=?", (reservation,)).fetchone()
            if row is None or row[0] != "reserved":
                raise ValueError("unknown or already settled reservation")
            self.db.execute("UPDATE budget SET actual=?,status='settled' WHERE reservation_id=?",
                            (actual_microusd, reservation))

    def budget(self) -> dict:
        with self.lock:
            used, reported = self.db.execute(
                "SELECT COALESCE(SUM(COALESCE(actual,reserved)),0),COALESCE(SUM(actual),0) FROM budget"
            ).fetchone()
        return {"cap_microusd": self.cap, "committed_microusd": used, "reported_microusd": reported,
                "remaining_microusd": max(0, self.cap - used),
                "scope": "v2 local reservations, not the shared provider account"}
