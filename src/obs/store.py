"""Everything that happened on a call, while it happens and afterwards.

One store holds the live calls the console watches and writes the same events
to SQLite, so a call can be explained again long after the socket closed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.config import settings

MAX_EVENTS_IN_MEMORY = 600
MAX_FINISHED_CALLS = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class LiveCall:
    call_id: str
    from_number: Optional[str] = None
    started_at: str = field(default_factory=_now_iso)
    started_monotonic: float = field(default_factory=time.monotonic)
    ended_at: Optional[str] = None
    status: str = "ringing"
    stage: str = "greeting"
    language: str = "es"
    language_confidence: Optional[float] = None
    language_source: Optional[str] = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS_IN_MEMORY))
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    clinic_calls: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    submissions: list[dict[str, Any]] = field(default_factory=list)
    patient: Optional[dict[str, Any]] = None
    intent: Optional[str] = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=lambda: {
        "turns": 0,
        "interruptions": 0,
        "tool_calls": 0,
        "clinic_calls": 0,
        "llm_ms": [],
        "tts_first_byte_ms": [],
        "response_ms": [],
        "tokens_in": 0,
        "tokens_out": 0,
    })
    seq: int = 0

    @property
    def duration_s(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 1)

    def summary(self) -> dict[str, Any]:
        latencies = self.metrics["response_ms"]
        return {
            "call_id": self.call_id,
            "from_number": self.from_number,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "stage": self.stage,
            "language": self.language,
            "language_confidence": self.language_confidence,
            "language_source": self.language_source,
            "duration_s": self.duration_s if self.ended_at is None else self._final_duration(),
            "turns": self.metrics["turns"],
            "interruptions": self.metrics["interruptions"],
            "tool_calls": self.metrics["tool_calls"],
            "clinic_calls": self.metrics["clinic_calls"],
            "median_response_ms": _median(latencies),
            "patient": _patient_line(self.patient),
            "intent": self.intent,
            "actions": [
                {"action": s["action"], "accepted": s.get("accepted"), "status": s.get("status")}
                for s in self.submissions
            ],
            "errors": len(self.errors),
        }

    def _final_duration(self) -> float:
        try:
            started = datetime.fromisoformat(self.started_at)
            ended = datetime.fromisoformat(self.ended_at or self.started_at)
            return round((ended - started).total_seconds(), 1)
        except ValueError:
            return 0.0

    def detail(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "transcript": self.transcript,
            "tool_calls": self.tool_calls,
            "clinic_calls": self.clinic_calls,
            "decisions": self.decisions,
            "submissions": self.submissions,
            "patient_full": self.patient,
            "errors": self.errors,
            "events": list(self.events),
            "metrics": {
                **self.metrics,
                "median_llm_ms": _median(self.metrics["llm_ms"]),
                "median_tts_first_byte_ms": _median(self.metrics["tts_first_byte_ms"]),
            },
        }


class CallStore:
    """Live registry, event fan-out and durable history in one place."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._live: dict[str, LiveCall] = {}
        self._finished: deque = deque(maxlen=MAX_FINISHED_CALLS)
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._peak_concurrency = 0
        self._db_path = db_path or settings.db_path
        self._db: Optional[sqlite3.Connection] = None
        self._init_db()

    # ---- database ----------------------------------------------------

    def _init_db(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                call_id TEXT PRIMARY KEY,
                from_number TEXT,
                started_at TEXT,
                ended_at TEXT,
                status TEXT,
                summary TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT,
                seq INTEGER,
                ts TEXT,
                kind TEXT,
                payload TEXT
            );
            CREATE INDEX IF NOT EXISTS events_call_idx ON events(call_id, seq);
            """
        )
        self._db.commit()

    def _write_event(self, call_id: str, seq: int, ts: str, kind: str, payload: dict[str, Any]) -> None:
        if self._db is None:
            return
        with self._lock:
            self._db.execute(
                "INSERT INTO events (call_id, seq, ts, kind, payload) VALUES (?, ?, ?, ?, ?)",
                (call_id, seq, ts, kind, json.dumps(payload, ensure_ascii=False, default=str)),
            )
            self._db.commit()

    def _write_call(self, call: LiveCall) -> None:
        if self._db is None:
            return
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO calls (call_id, from_number, started_at, ended_at, status, summary)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    call.call_id,
                    call.from_number,
                    call.started_at,
                    call.ended_at,
                    call.status,
                    json.dumps(call.summary(), ensure_ascii=False, default=str),
                ),
            )
            self._db.commit()

    # ---- live calls --------------------------------------------------

    def open_call(self, call_id: str, from_number: Optional[str]) -> LiveCall:
        call = LiveCall(call_id=call_id, from_number=from_number, status="live")
        self._live[call_id] = call
        # Sampled here rather than when the console asks, or a burst that has
        # already ended would never show up as one.
        self._peak_concurrency = max(self._peak_concurrency, len(self._live))
        self._write_call(call)
        return call

    def get(self, call_id: str) -> Optional[LiveCall]:
        if call_id in self._live:
            return self._live[call_id]
        for call in self._finished:
            if call.call_id == call_id:
                return call
        return None

    def close_call(self, call_id: str, status: str = "finished") -> Optional[LiveCall]:
        call = self._live.pop(call_id, None)
        if call is None:
            return None
        call.status = status
        call.ended_at = _now_iso()
        self._finished.appendleft(call)
        self._write_call(call)
        return call

    def live_calls(self) -> list[LiveCall]:
        return sorted(self._live.values(), key=lambda c: c.started_at)

    def recent_calls(self) -> list[LiveCall]:
        return list(self._finished)

    # ---- events ------------------------------------------------------

    async def record(self, call_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Log one thing that happened, update the call, and fan it out."""
        call = self.get(call_id)
        if call is None:
            return

        call.seq += 1
        event = {"call_id": call_id, "seq": call.seq, "ts": _now_iso(), "kind": kind, "payload": payload}
        call.events.append(event)
        self._apply(call, kind, payload)
        self._write_event(call_id, call.seq, event["ts"], kind, payload)
        await self._publish({"type": "event", "event": event, "summary": call.summary()})

    def _apply(self, call: LiveCall, kind: str, payload: dict[str, Any]) -> None:
        """Keep the call's own view of itself in step with its events."""
        if kind == "stt_final":
            call.transcript.append({
                "role": "caller", "text": payload.get("text", ""),
                "ts": _now_iso(), "language": payload.get("language"),
            })
            if payload.get("language"):
                call.language = payload["language"]
            if payload.get("language_confidence") is not None:
                call.language_confidence = payload["language_confidence"]
            if payload.get("language_source"):
                call.language_source = payload["language_source"]
        elif kind == "language_detected":
            if payload.get("language"):
                call.language = payload["language"]
            if payload.get("confidence") is not None:
                call.language_confidence = payload["confidence"]
            if payload.get("source"):
                call.language_source = payload["source"]
        elif kind == "agent_said":
            call.transcript.append({"role": "agent", "text": payload.get("text", ""), "ts": _now_iso()})
            call.metrics["turns"] += 1
            if payload.get("response_ms"):
                call.metrics["response_ms"].append(payload["response_ms"])
        elif kind == "interruption":
            call.metrics["interruptions"] += 1
        elif kind == "tool_call":
            call.tool_calls.append({**payload, "ts": _now_iso()})
            call.metrics["tool_calls"] += 1
        elif kind == "clinic_call":
            call.clinic_calls.append({**payload, "ts": _now_iso()})
            call.metrics["clinic_calls"] += 1
        elif kind == "decision":
            call.decisions.append({**payload, "ts": _now_iso()})
            if payload.get("stage"):
                call.stage = payload["stage"]
        elif kind == "submit":
            call.submissions.append({**payload, "ts": _now_iso()})
        elif kind == "patient_identified":
            call.patient = payload.get("patient")
        elif kind == "intent":
            call.intent = payload.get("intent")
        elif kind == "llm":
            if payload.get("elapsed_ms"):
                call.metrics["llm_ms"].append(payload["elapsed_ms"])
            call.metrics["tokens_in"] += payload.get("tokens_in", 0) or 0
            call.metrics["tokens_out"] += payload.get("tokens_out", 0) or 0
        elif kind == "tts":
            if payload.get("first_byte_ms"):
                call.metrics["tts_first_byte_ms"].append(payload["first_byte_ms"])
        elif kind == "error":
            call.errors.append({**payload, "ts": _now_iso()})

    # ---- fan-out -----------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=400)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def _publish(self, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A console that cannot keep up loses frames, not the call.
                self._subscribers.discard(queue)

    async def announce(self, message: dict[str, Any]) -> None:
        await self._publish(message)

    # ---- history -----------------------------------------------------

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT call_id, from_number, started_at, ended_at, status, summary"
                " FROM calls ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for call_id, from_number, started_at, ended_at, status, summary in rows:
            try:
                parsed = json.loads(summary) if summary else {}
            except ValueError:
                parsed = {}
            parsed.update({
                "call_id": call_id, "from_number": from_number,
                "started_at": started_at, "ended_at": ended_at, "status": status,
            })
            out.append(parsed)
        return out

    def replay(self, call_id: str) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, ts, kind, payload FROM events WHERE call_id = ? ORDER BY seq",
                (call_id,),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for seq, ts, kind, payload in rows:
            try:
                parsed = json.loads(payload) if payload else {}
            except ValueError:
                parsed = {}
            events.append({"seq": seq, "ts": ts, "kind": kind, "payload": parsed})
        return events

    def aggregate(self) -> dict[str, Any]:
        live = self.live_calls()
        recent = self.recent_calls()
        all_latencies: list[int] = []
        for call in live + recent:
            all_latencies.extend(call.metrics["response_ms"])

        submitted = [call for call in recent if call.submissions]
        accepted = [
            call for call in recent
            if any(s.get("accepted") for s in call.submissions)
        ]
        return {
            "live": len(live),
            "peak_concurrency": self._peak_concurrency,
            "recent": len(recent),
            "with_submission": len(submitted),
            "with_accepted_submission": len(accepted),
            "silent_calls": len([c for c in recent if not c.submissions]),
            "median_response_ms": _median(all_latencies),
            "p90_response_ms": _percentile(all_latencies, 90),
        }


def _median(values: list[int]) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _percentile(values: list[int], pct: int) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def _patient_line(patient: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not patient:
        return None
    return {
        "patient_id": patient.get("patient_id"),
        "name": patient.get("full_name")
        or " ".join(
            str(patient.get(k, "")) for k in ("given_name", "first_surname", "second_surname")
        ).strip(),
        "insurer": patient.get("insurer"),
        "has_visited_before": patient.get("has_visited_before"),
    }


store = CallStore()
