"""A canned dry-run trace so /demo has a story if the floor is quiet.

Every field is derived by src.obs.product.overlay from these events — the same
function a live call uses. The patients are the synthetic challenge directory.
"""

from __future__ import annotations

from typing import Any

from src.obs.product import overlay

EVENTS: list[dict[str, Any]] = [
    {"kind": "call_started", "ts": "2026-09-20T10:01:00+02:00", "payload": {"from_number": "+34600111222"}},
    {"kind": "language_detected", "ts": "2026-09-20T10:01:04+02:00",
     "payload": {"language": "es", "source": "text_markers"}},
    {"kind": "stt_final", "ts": "2026-09-20T10:01:08+02:00",
     "payload": {"text": "Buenos días, soy Elena García, quiero dermatología el martes por la mañana.",
                 "language": "es"}},
    {"kind": "tool_call", "ts": "2026-09-20T10:01:10+02:00",
     "payload": {"name": "lookup_patient", "arguments": {"name": "Elena García"},
                 "result": {"found": 1, "match": {"patient_id": "P-DEMO", "full_name": "Elena García"}}}},
    {"kind": "patient_identified", "ts": "2026-09-20T10:01:12+02:00",
     "payload": {
         "visit_count": 11,
         "patient": {
             "patient_id": "P-DEMO",
             "full_name": "Elena García",
             "national_id": "12345678Z",
             "phone": "600111222",
             "email": "elena.garcia@example.com",
             "insurer": "DKV",
             "date_of_birth": "1984-03-12",
             "note": "Regular patient. Hard of hearing — speak clearly.",
             "has_visited_before": True,
         },
     }},
    {"kind": "tool_call", "ts": "2026-09-20T10:01:13+02:00",
     "payload": {"name": "open_chart", "arguments": {"patient_id": "P-DEMO"},
                 "result": {
                     "visit_count": 11,
                     "last_visit": {"provider_name": "Dra. Iglesias", "when": "2025-03-04 10:00",
                                    "location_name": "Arenal Norte"},
                     "upcoming": [],
                     "recent_past": [],
                 }}},
    {"kind": "interruption", "ts": "2026-09-20T10:01:20+02:00", "payload": {"heard": "mejor el Dr. Vilar"}},
    {"kind": "tool_call", "ts": "2026-09-20T10:01:22+02:00",
     "payload": {"name": "find_appointments",
                 "arguments": {"when": "Tuesday morning", "provider_name": "Iglesias"},
                 "result": {
                     "asked_for": "Tuesday morning",
                     "notes": ["Dra. Iglesias does not take this patient's plan"],
                     "options": [],
                 }}},
    {"kind": "tool_call", "ts": "2026-09-20T10:01:28+02:00",
     "payload": {"name": "find_appointments",
                 "arguments": {"when": "Tuesday morning", "provider_name": "Vilar"},
                 "result": {
                     "asked_for": "Tuesday morning",
                     "notes": [],
                     "options": [{"when": "Tuesday 10:30", "doctor": "Dr. Vilar", "site": "Arenal Norte"}],
                 }}},
    {"kind": "tool_call", "ts": "2026-09-20T10:01:40+02:00",
     "payload": {"name": "book_slot", "arguments": {"option": 1, "caller_confirmed": True},
                 "result": {"booked": True, "confirmed": {
                     "when": "Tuesday 10:30", "doctor": "Dr. Vilar", "site": "norte"}}}},
    {"kind": "decision", "ts": "2026-09-20T10:01:41+02:00",
     "payload": {"stage": "booked", "why": "Caller accepted the earliest valid alternative after Dra. Iglesias refused DKV."}},
    {"kind": "submit", "ts": "2026-09-20T10:01:42+02:00",
     "payload": {"action": "book", "accepted": True, "status": 200, "dry_run": True,
                 "payload": {"slot": "2026-09-22T10:30:00+02:00", "provider_id": "PR-VILAR",
                             "location_id": "norte", "patient_id": "P-DEMO"}}},
    {"kind": "followup_email", "ts": "2026-09-20T10:01:43+02:00",
     "payload": {"sent": True, "ics": True, "maps_url": "https://www.google.com/maps/search/?api=1&query=Madrid",
                 "subject": "Su cita está confirmada", "action": "book"}},
    {"kind": "language_detected", "ts": "2026-09-20T10:01:50+02:00",
     "payload": {"language": "ca", "source": "text_markers"}},
]


def story() -> dict[str, Any]:
    tools = [e["payload"] for e in EVENTS if e["kind"] == "tool_call"]
    submissions = [e["payload"] for e in EVENTS if e["kind"] == "submit"]
    decisions = [e["payload"] for e in EVENTS if e["kind"] == "decision"]
    followups = [e["payload"] for e in EVENTS if e["kind"] == "followup_email"]
    identified = next(e["payload"] for e in EVENTS if e["kind"] == "patient_identified")
    chart = next(e["payload"]["result"] for e in EVENTS if e["kind"] == "tool_call" and e["payload"]["name"] == "open_chart")
    product = overlay(
        events=EVENTS,
        tool_calls=tools,
        submissions=submissions,
        decisions=decisions,
        patient=identified["patient"],
        chart=chart,
        language="ca",
        metrics={"interruptions": 1, "response_ms": [820, 640]},
        status="rehearsed",
        duration_s=74,
        from_number="+34600111222",
        followups=followups,
    )
    return {
        "call_id": "demo-elena-garcia",
        "label": "Example dry-run · Elena García",
        "synthetic": True,
        "from_number": "+34600111222",
        "language": "ca",
        "status": "rehearsed",
        "duration_s": 74,
        "patient": identified["patient"],
        "patient_full": identified["patient"],
        "tool_calls": tools,
        "submissions": submissions,
        "decisions": decisions,
        "product": product,
        "transcript": [
            {"role": "agent", "text": "Clínica Arenal, buenos días. ¿En qué puedo ayudarle?"},
            {"role": "caller", "text": "Buenos días, soy Elena García, quiero dermatología el martes por la mañana."},
            {"role": "caller", "cut": True, "text": "⟨cuts the agent⟩ mejor el Dr. Vilar"},
            {"role": "agent", "text": "El martes a las diez y media con el Dr. Vilar en Arenal Norte. ¿Se la reservo?"},
            {"role": "caller", "text": "Sí, perfecto. Moltes gràcies."},
            {"role": "agent", "text": "Reservada. Li envio la confirmació. Necessita res més?"},
        ],
    }
