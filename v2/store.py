from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


class BudgetExceeded(RuntimeError):
    pass


LIFECYCLE = {"session_started", "call_started", "call_ended", "session_ended"}
STATUSES = {"active", "completed", "disconnected", "error", "timed_out"}
ACTIONS = {"book", "cancel", "reschedule", "register", "no_action", "escalate"}


def _identifier(value, name="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError(f"invalid {name}")
    return value


def _json(value, maximum=1_048_576):
    def validate(item, depth=0):
        if depth > 32:
            raise ValueError("JSON nesting exceeds limit")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON keys must be strings")
            for child in item.values():
                validate(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child, depth + 1)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise ValueError("value must be JSON data")
    validate(value)
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(result.encode("utf-8")) > maximum:
        raise ValueError("JSON data exceeds size limit")
    return result


def _money(value, positive=False):
    if type(value) is not int or value < (1 if positive else 0) or value > 2**63 - 1:
        raise ValueError("cost must be an integer number of nonnegative microusd")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _accumulate(summary, kind, payload, event_id):
    counts = summary.setdefault("counts", {})
    counts[kind] = counts.get(kind, 0) + 1
    if kind in LIFECYCLE and isinstance(payload.get("status"), str) and payload["status"] in STATUSES:
        status = payload["status"]
        previous = summary.get("status")
        priority = {"active": 0, "completed": 1, "disconnected": 2, "timed_out": 3, "error": 4}
        if priority[status] >= priority.get(previous, -1):
            summary["status"] = status
    if kind == "action_receipt":
        receipts = summary.setdefault("receipts", {})
        key = payload.get("action_key") or event_id
        receipts[key] = {k: payload.get(k) for k in ("accepted", "source")}
    if kind == "response_planned":
        elapsed = payload.get("elapsed_ms")
        if type(elapsed) in (int, float) and 0 <= elapsed <= 86_400_000 and math.isfinite(elapsed):
            summary["response_ms"] = [*summary.get("response_ms", []), elapsed][-1000:]
    if kind == "audio_output":
        summary["audio"] = {key: payload[key] for key in (
            "audio_status", "finalized", "frames_sent", "non_silent_frames_sent", "bytes_sent",
            "first_audio_ms", "first_non_silent_ms", "outbound_frames", "non_silent_frames",
            "frames", "outbound_non_silent_frames", "first_signal_ms", "observation",
        ) if key in payload}
    if kind in {"fixture_grade", "model_assessment", "failure_review"}:
        summary[kind] = payload
    return summary


def projected_metrics(state, summary, costs=None):
    counts = summary.get("counts", {})
    intents = list(state.get("intents", {}).values())
    receipts = list(summary.get("receipts", {}).values())
    audio = summary.get("audio")
    audio_status = "not_measured"
    if audio is not None:
        audio_status = audio.get("audio_status", "unknown")
        if not isinstance(audio_status, str) or audio_status not in {"unknown", "not_measured", "not_applicable", "silent", "non_silent", "signal_sent", "sent"}:
            audio_status = "unknown"
        if audio_status == "silent" and audio.get("finalized") is not True:
            audio_status = "unknown"
    resolved = sum(i.get("status") == "completed" for i in intents)
    status = summary.get("status", state.get("status", "unknown"))
    terminal = status in STATUSES - {"active"}
    return {
        "intents": len(intents), "resolved": resolved,
        "all_intents_completed": (resolved == len(intents)) if intents else None,
        "submission_attempts": max(summary.get("action_attempts", 0), len(receipts)),
        "unresolved_actions": summary.get("unresolved_actions", 0),
        "missing_submission": not bool(receipts or summary.get("action_attempts")) if terminal else None,
        "http_accepted": sum(r.get("source") == "platform_receipt" and r.get("accepted") is True for r in receipts),
        "simulated_receipts": sum(r.get("source") == "simulation" for r in receipts),
        "audio_status": audio_status, "audio_observation": audio,
        "response_ms": summary.get("response_ms", []), "response_ms_kind": "planning_not_caller_playback",
        "meaningful_response_ms": None, "entity_errors": None,
        "interruptions": counts.get("interruption", 0),
        "guard_rejections": counts.get("guard_rejected", 0),
        "provider_failures": counts.get("provider_error", 0),
        "errors": sum(counts.get(k, 0) for k in ("error", "provider_error", "pipeline_error")),
        "cost": costs, "official_grade": None,
    }


def report_metrics(report):
    summary = {}
    for index, event in enumerate(report.get("events", [])):
        _accumulate(summary, event["kind"], event.get("payload", {}), event.get("event_id", str(index)))
    if "actions" in report:
        summary["action_attempts"] = len(report["actions"])
        summary["unresolved_actions"] = sum(a.get("status") in {"pending", "unresolved"} for a in report["actions"])
    return projected_metrics(report.get("state", {}), summary, report.get("cost"))


class RunStore:
    def __init__(self, path: str = ":memory:", cap_microusd: int = 30_000_000):
        _money(cap_microusd)
        if cap_microusd > 30_000_000:
            raise ValueError("this project's authorized cap is $30")
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
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
            CREATE TABLE IF NOT EXISTS store_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state_versions (
                run_id TEXT NOT NULL, version INTEGER NOT NULL, timestamp TEXT NOT NULL,
                state TEXT NOT NULL, PRIMARY KEY(run_id, version)
            );
            CREATE TABLE IF NOT EXISTS run_projections (run_id TEXT PRIMARY KEY, summary TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evaluation_leases (
                name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_details (
                action_key TEXT PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT, action_key TEXT NOT NULL,
                timestamp TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT, evidence TEXT
            );
            CREATE INDEX IF NOT EXISTS runs_keyset ON runs(created_at DESC, run_id DESC);
            CREATE INDEX IF NOT EXISTS budget_run ON budget(run_id);
            CREATE INDEX IF NOT EXISTS actions_run ON actions(run_id);
        """)
        with self._transaction():
            self.db.execute("INSERT OR IGNORE INTO store_metadata VALUES ('schema_version','2')")
            self.db.execute("INSERT OR IGNORE INTO store_metadata VALUES ('cap_microusd',?)", (str(self.cap),))
            self.db.execute("UPDATE store_metadata SET value=? WHERE key='cap_microusd' AND CAST(value AS INTEGER)>?",
                            (str(self.cap), self.cap))
            rows = self.db.execute("SELECT run_id,state FROM runs WHERE run_id NOT IN (SELECT run_id FROM run_projections)").fetchall()
            for row in rows:
                summary = {}
                for event in self.db.execute("SELECT * FROM events WHERE run_id=? ORDER BY sequence", (row["run_id"],)):
                    _accumulate(summary, event["kind"], json.loads(event["payload"]), event["event_id"])
                self.db.execute("INSERT INTO run_projections VALUES (?,?)", (row["run_id"], _json(summary)))
                self.db.execute("INSERT OR IGNORE INTO state_versions VALUES (?,0,?,?)", (row["run_id"], _now(), row["state"]))

    @contextmanager
    def _transaction(self, immediate=True):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def save(self, state, manifest: dict | None = None):
        from v2.models import CallState
        state = CallState.model_validate(state.model_dump(mode="json"))
        encoded = _json(state.model_dump(mode="json"))
        if manifest is not None and not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        _json(manifest or {}, 65_536)
        with self._transaction():
            row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (state.run_id,)).fetchone()
            if row:
                if row["call_id"] != state.call_id or row["mode"] != state.mode:
                    raise ValueError("run identity and mode are immutable")
                previous_state = json.loads(row["state"])
                if state.revision < previous_state.get("revision", 0) or state.turn < previous_state.get("turn", 0):
                    raise ValueError("stale call state cannot replace a newer revision")
                original = json.loads(row["manifest"])
                if any(k in original and original[k] != v for k, v in (manifest or {}).items()):
                    raise ValueError("run provenance cannot be overwritten")
                original.update(manifest or {})
                self.db.execute("UPDATE runs SET state=?,manifest=? WHERE run_id=?", (encoded, _json(original, 65_536), state.run_id))
            else:
                self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (
                    state.run_id, state.call_id, state.mode, _now(), encoded, _json(manifest or {})))
                self.db.execute("INSERT OR IGNORE INTO run_projections VALUES (?,?)", (state.run_id, "{}"))
            previous = self.db.execute("SELECT version,state FROM state_versions WHERE run_id=? ORDER BY version DESC LIMIT 1", (state.run_id,)).fetchone()
            if previous is None or previous["state"] != encoded:
                version = previous["version"] + 1 if previous else 0
                self.db.execute("INSERT INTO state_versions VALUES (?,?,?,?)", (state.run_id, version, _now(), encoded))

    def event(self, run_id: str, kind: str, payload: dict):
        _identifier(run_id, "run_id")
        _identifier(kind, "event kind")
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        encoded = _json(payload, 262_144)
        if kind in LIFECYCLE and "status" in payload:
            if not isinstance(payload["status"], str) or payload["status"] not in STATUSES or (kind.endswith("ended") and payload["status"] == "active"):
                raise ValueError("invalid lifecycle status")
        if kind == "action_receipt":
            if type(payload.get("accepted")) is not bool or not isinstance(payload.get("payload"), dict):
                raise ValueError("action receipt requires a boolean outcome and action payload")
            if payload.get("source") not in ("simulation", "platform_receipt") or payload.get("action") not in tuple(ACTIONS):
                raise ValueError("invalid action receipt provenance")
            if "action_key" in payload:
                _identifier(payload["action_key"], "receipt action key")
        event_id = uuid4().hex
        with self._transaction():
            run = self.db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                action = self.db.execute("SELECT 1 FROM actions WHERE run_id=? AND action_key=?", (run_id, payload.get("action_key"))).fetchone() if kind == "action_receipt" else None
                if action is None:
                    raise ValueError("unknown run")
                self.db.execute("INSERT OR IGNORE INTO run_projections VALUES (?,?)", (run_id, "{}"))
            sequence = self.db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (event_id, run_id, sequence, _now(), kind, encoded))
            row = self.db.execute("SELECT summary FROM run_projections WHERE run_id=?", (run_id,)).fetchone()
            summary = _accumulate(json.loads(row[0]), kind, payload, event_id)
            self.db.execute("UPDATE run_projections SET summary=? WHERE run_id=?", (_json(summary), run_id))
        return event_id

    def finish_run(self, run_id: str, status: str, *, reason: str | None = None):
        if not isinstance(status, str) or status not in STATUSES - {"active"}:
            raise ValueError("terminal lifecycle status required")
        if reason is not None:
            _identifier(reason, "termination reason")
        return self.event(run_id, "session_ended", {"status": status, "reason": reason})

    def _cost(self, run_id):
        committed, actual, reserved, count = self.db.execute(
            "SELECT COALESCE(SUM(COALESCE(actual,reserved)),0),SUM(actual),"
            "COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0),COUNT(*) FROM budget WHERE run_id=?",
            (run_id,)).fetchone()
        return {"committed_microusd": committed, "actual_microusd": None if reserved else (actual or 0),
                "reported_microusd": actual or 0, "reserved_microusd": reserved, "actual_complete": reserved == 0}

    def _summary(self, row):
        state, summary = json.loads(row["state"]), json.loads(row["summary"])
        attempts, unresolved = self.db.execute("SELECT COUNT(*),COALESCE(SUM(status IN ('pending','unresolved')),0) FROM actions WHERE run_id=?", (row["run_id"],)).fetchone()
        summary.update(action_attempts=attempts, unresolved_actions=unresolved)
        return {"run_id": row["run_id"], "call_id": row["call_id"], "mode": row["mode"],
                "language": state.get("language"), "created_at": row["created_at"],
                "status": summary.get("status", state.get("status", "unknown")),
                "metrics": projected_metrics(state, summary, self._cost(row["run_id"])), "official_grade": None}

    def report(self, run_id: str) -> dict | None:
        _identifier(run_id, "run_id")
        with self._transaction(immediate=False):
            row = self.db.execute("SELECT runs.*,summary FROM runs JOIN run_projections USING(run_id) WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            result = self._summary(row)
            events = self.db.execute("SELECT * FROM events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
            actions = self.db.execute("SELECT * FROM actions WHERE run_id=? ORDER BY action_key", (run_id,)).fetchall()
            cost = self._cost(run_id)
        projection = json.loads(row["summary"])
        return {**result, "schema_version": 2, "manifest": json.loads(row["manifest"]),
                "state": json.loads(row["state"]), "cost": cost,
                "fixture_grade": projection.get("fixture_grade"), "model_assessment": projection.get("model_assessment"),
                "failure_review": projection.get("failure_review"),
                "actions": [{**dict(a), "receipt": json.loads(a["receipt"]) if a["receipt"] else None} for a in actions],
                "events": [{**dict(e), "payload": json.loads(e["payload"])} for e in events]}

    def list_runs(self, limit: int = 50, before: str | None = None) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        params = []
        where = ""
        if before is not None:
            try:
                if not isinstance(before, str) or len(before) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", before):
                    raise ValueError("invalid cursor")
                stamp, run_id = json.loads(base64.urlsafe_b64decode(before + "=" * (-len(before) % 4)))
                _identifier(run_id, "cursor run_id")
                if not isinstance(stamp, str) or len(stamp) > 40 or datetime.fromisoformat(stamp).utcoffset() is None:
                    raise ValueError("invalid cursor time")
                where = "WHERE (created_at,run_id)<(?,?)"
                params = [stamp, run_id]
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError("invalid run cursor") from exc
        with self._transaction(immediate=False):
            rows = self.db.execute(f"""SELECT run_id,call_id,mode,created_at,summary,
                json_object('language',json_extract(state,'$.language'),
                    'status',COALESCE(json_extract(state,'$.status'),'unknown'),
                    'intents',json((SELECT json_group_object(key,json_object('status',json_extract(value,'$.status')))
                                   FROM json_each(runs.state,'$.intents')))) AS state
                FROM runs JOIN run_projections USING(run_id) {where}
                ORDER BY created_at DESC,run_id DESC LIMIT ?""", (*params, limit + 1)).fetchall()
            result = [self._summary(row) for row in rows[:limit]]
        cursor = None
        if len(rows) > limit:
            row = rows[limit - 1]
            cursor = base64.urlsafe_b64encode(_json([row["created_at"], row["run_id"]]).encode()).decode().rstrip("=")
        return {"runs": result, "next_cursor": cursor}

    def begin_action(self, run_id: str, intent_id: str, action: str, payload: dict) -> tuple[str, dict | None]:
        _identifier(run_id, "run_id")
        _identifier(intent_id, "intent_id")
        if not isinstance(action, str) or action not in ACTIONS or not isinstance(payload, dict):
            raise ValueError("invalid action")
        _json(payload, 65_536)
        canonical = json.dumps([run_id, action, payload], sort_keys=True, separators=(",", ":"), allow_nan=False)
        key = hashlib.sha256(canonical.encode()).hexdigest()
        with self._transaction():
            row = self.db.execute("SELECT status,receipt FROM actions WHERE action_key=?", (key,)).fetchone()
            if row:
                if row["status"] in {"accepted", "rejected"}:
                    return key, json.loads(row["receipt"])
                raise RuntimeError("action outcome unresolved; reconcile before retrying")
            self.db.execute("INSERT INTO actions VALUES (?,?,?,?,NULL)", (key, run_id, intent_id, "pending"))
            self.db.execute("INSERT INTO action_details VALUES (?,?,?)", (key, action, _json(payload)))
            self.db.execute("INSERT INTO action_history(action_key,timestamp,status) VALUES (?,?,'pending')", (key, _now()))
        return key, None

    def _finish_action(self, key, receipt, evidence=None):
        _identifier(key, "action key")
        if not isinstance(receipt, dict) or type(receipt.get("accepted")) is not bool:
            raise ValueError("receipt requires boolean accepted")
        encoded = _json(receipt, 65_536)
        with self._transaction():
            row = self.db.execute("SELECT * FROM actions WHERE action_key=?", (key,)).fetchone()
            if row is None:
                raise ValueError("unknown action")
            if receipt.get("action_key", key) != key:
                raise ValueError("receipt action key mismatch")
            detail = self.db.execute("SELECT * FROM action_details WHERE action_key=?", (key,)).fetchone()
            if detail and (receipt.get("action", detail["action"]) != detail["action"] or
                           receipt.get("payload", json.loads(detail["payload"])) != json.loads(detail["payload"])):
                raise ValueError("receipt does not match the reserved action")
            if row["receipt"] and json.loads(row["receipt"]) == receipt and (row["status"] != "unresolved" or evidence is None):
                return
            if row["status"] in {"accepted", "rejected"}:
                raise ValueError("a resolved action receipt is immutable")
            if row["status"] == "unresolved" and evidence is None:
                raise ValueError("unresolved actions require explicit reconciliation evidence")
            status = "accepted" if receipt["accepted"] else ("rejected" if evidence else "unresolved")
            self.db.execute("UPDATE actions SET status=?,receipt=? WHERE action_key=?", (status, encoded, key))
            self.db.execute("INSERT INTO action_history(action_key,timestamp,status,receipt,evidence) VALUES (?,?,?,?,?)",
                            (key, _now(), status, encoded, evidence))

    def finish_action(self, key: str, receipt: dict):
        self._finish_action(key, receipt)

    def reconcile_action(self, key: str, receipt: dict, *, evidence: str):
        if not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 500:
            raise ValueError("bounded reconciliation evidence is required")
        self._finish_action(key, receipt, evidence)

    def claim_experiment(self, owner: str, ttl_seconds: int = 180):
        _identifier(owner, "experiment owner")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 600:
            raise ValueError("experiment lease must last 1 to 600 seconds")
        now = datetime.now(timezone.utc)
        with self._transaction():
            row = self.db.execute("SELECT * FROM evaluation_leases WHERE name='paid_duel'").fetchone()
            if row and row["expires_at"] > now.isoformat():
                raise RuntimeError("a paid evaluation is already active on this ledger")
            self.db.execute("INSERT INTO evaluation_leases VALUES ('paid_duel',?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at",
                            (owner, (now + timedelta(seconds=ttl_seconds)).isoformat()))

    def release_experiment(self, owner: str):
        _identifier(owner, "experiment owner")
        with self._transaction():
            self.db.execute("UPDATE evaluation_leases SET expires_at=? WHERE name='paid_duel' AND owner=?", (_now(), owner))

    def reserve(self, run_id: str, service: str, microusd: int) -> str:
        _identifier(run_id, "run_id")
        _identifier(service, "service")
        _money(microusd, positive=True)
        reservation = uuid4().hex
        with self._transaction():
            cap = min(self.cap, int(self.db.execute("SELECT value FROM store_metadata WHERE key='cap_microusd'").fetchone()[0]))
            used = self.db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM budget").fetchone()[0]
            if used + microusd > cap:
                raise BudgetExceeded("authorized API budget exhausted")
            self.db.execute("INSERT INTO budget VALUES (?,?,?,?,NULL,?)", (reservation, run_id, service, microusd, "reserved"))
        return reservation

    def settle(self, reservation: str, actual_microusd: int):
        _identifier(reservation, "reservation")
        _money(actual_microusd)
        with self._transaction():
            row = self.db.execute("SELECT status,actual,reserved FROM budget WHERE reservation_id=?", (reservation,)).fetchone()
            if row is None:
                raise ValueError("unknown reservation")
            if row["status"] == "settled":
                if row["actual"] == actual_microusd:
                    return
                raise ValueError("actual cost is immutable after reconciliation")
            committed = self.db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM budget").fetchone()[0]
            if committed - row["reserved"] + actual_microusd > 2**63 - 1:
                raise ValueError("actual cost exceeds the ledger integer range")
            self.db.execute("UPDATE budget SET actual=?,status='settled' WHERE reservation_id=?", (actual_microusd, reservation))

    def budget(self) -> dict:
        with self._transaction(immediate=False):
            used, reported, reserved, pending = self.db.execute(
                "SELECT COALESCE(SUM(COALESCE(actual,reserved)),0),COALESCE(SUM(actual),0),"
                "COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0),"
                "SUM(CASE WHEN actual IS NULL THEN 1 ELSE 0 END) FROM budget"
            ).fetchone()
            cap = min(self.cap, int(self.db.execute("SELECT value FROM store_metadata WHERE key='cap_microusd'").fetchone()[0]))
        return {"cap_microusd": cap, "committed_microusd": used, "reported_microusd": reported,
                "reserved_microusd": reserved, "unsettled_reservations": pending or 0,
                "actual_complete": not pending, "remaining_microusd": max(0, cap - used),
                "over_cap_microusd": max(0, used - cap),
                "scope": "v2 local reservations, not the shared provider account"}
