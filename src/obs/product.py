"""Product views derived from events the call already emitted.

The model still conducts the conversation and the engine still decides the
record. This module only translates that trace into something a person on the
floor — or a juror — can read: a journey, intents, a why, a safety shield,
patient context and a post-call summary. Nothing here invents a fact.
"""

from __future__ import annotations

from typing import Any, Optional

LANGUAGE_NAMES = {"es": "Spanish", "en": "English", "ca": "Catalan"}

_HEARING = (
    "hard of hearing", "hearing", "sordo", "sorda", "hipoacus", "hearing aid",
    "does not hear well", "no oye", "no sent",
)

_PRIVACY = ("protected", "national_id", "do not read", "another patient", "wrong chart")
_REFERRAL = ("referral", "referred")
_INSURANCE = ("does not take", "does not cover", "plan", "insurer", "policy", "payable")
_EMERGENCY = ("medical_emergency", "red flag", "escalate", "emergency")


def overlay(
    *,
    events: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    submissions: list[dict[str, Any]] | None = None,
    decisions: list[dict[str, Any]] | None = None,
    patient: dict[str, Any] | None = None,
    chart: dict[str, Any] | None = None,
    language: str = "es",
    metrics: dict[str, Any] | None = None,
    status: str = "live",
    duration_s: float = 0.0,
    from_number: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    followups: list[dict[str, Any]] | None = None,
    languages_seen: list[str] | None = None,
) -> dict[str, Any]:
    """One payload the dashboard can render. Pure: no I/O, no model."""
    events = list(events or [])
    tool_calls = list(tool_calls or [])
    submissions = list(submissions or [])
    decisions = list(decisions or [])
    metrics = metrics or {}
    followups = list(followups or [])
    errors = list(errors or [])
    if languages_seen is None:
        languages_seen = _languages_seen(events, language)

    journey = derive_journey(events, tool_calls, submissions, followups, language)
    intents = derive_intents(tool_calls, submissions, patient)
    why = derive_why(tool_calls, submissions, decisions, patient)
    safety = derive_safety(tool_calls, submissions, decisions, patient)
    context = derive_patient_context(patient, chart, events)
    resolution = derive_resolution(
        patient=patient,
        language=language,
        languages_seen=languages_seen,
        intents=intents,
        submissions=submissions,
        why=why,
        metrics=metrics,
        duration_s=duration_s,
        status=status,
        followups=followups,
        from_number=from_number,
        errors=errors,
    )
    return {
        "journey": journey,
        "intents": intents,
        "why": why,
        "safety": safety,
        "patient_context": context,
        "resolution": resolution,
        "languages_seen": languages_seen,
        "hearing_support": context.get("hearing_support", False),
    }


def derive_journey(
    events: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    submissions: list[dict[str, Any]],
    followups: list[dict[str, Any]],
    language: str,
) -> list[dict[str, Any]]:
    """Ordered steps that actually happened, never a static checklist."""
    steps: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(key: str, label: str, ts: Any = None, detail: str = "") -> None:
        if key in seen:
            return
        seen.add(key)
        steps.append({"id": key, "label": label, "detail": detail, "ts": ts, "state": "done"})

    add("connected", "Connected")
    searches = 0
    for event in events:
        kind = event.get("kind")
        payload = event.get("payload") or {}
        ts = event.get("ts")
        if kind in ("language_detected", "stt_final") and payload.get("language"):
            code = str(payload["language"])[:2]
            add(f"language:{code}", f"Language detected · {LANGUAGE_NAMES.get(code, code)}", ts)
        elif kind == "patient_identified":
            person = payload.get("patient") or {}
            name = _patient_name(person)
            add("patient", f"Patient identified · {name or 'on file'}", ts)
            visits = payload.get("visit_count")
            if visits:
                add("known", f"Chart context · {visits} previous visits", ts)
        elif kind == "interruption":
            add("interrupt", "Caller interrupted — agent yielded", ts)
        elif kind == "tool_call":
            name = payload.get("name") or ""
            result = payload.get("result") or {}
            args = payload.get("arguments") or {}
            if name == "open_chart":
                add("chart", "Chart opened", ts)
            elif name == "lookup_patient" and result.get("found") == 1:
                add("verified", "Identity verified", ts)
            elif name == "check_symptom" and result.get("escalate"):
                add("redflag", "Red flag · booking stopped", ts)
            elif name == "nearest_site":
                serving = result.get("nearest_serving") or {}
                add("nearest", "Nearest site", ts, serving.get("name") or serving.get("location_id") or "")
            elif name == "find_appointments":
                searches += 1
                asked = (result.get("asked_for") or args.get("when") or "").strip()
                if searches == 1:
                    add("search", "Availability searched", ts, asked)
                else:
                    add(f"search:{searches}", "Availability searched again", ts, asked)
                    add("changed", "Caller changed preference", ts, asked)
            elif name == "book_slot" and (result.get("booked") or result.get("confirmed")):
                confirmed = result.get("confirmed") or {}
                add("confirmed", "Option confirmed", ts, confirmed.get("when") or "")
            elif name == "escalate_call":
                add("escalated", "Escalated", ts, str(args.get("reason") or ""))
        elif kind == "submit":
            action = (payload.get("action") or "").upper()
            ok = payload.get("accepted") or payload.get("dry_run")
            if action:
                add(f"submit:{action.lower()}", f"{action} {'accepted' if ok else 'recorded'}", ts)
        elif kind == "followup_email":
            add("followup", "Confirmation sent", ts, "Email" if payload.get("sent") else "Calendar ready")

    if not any(s["id"].startswith("language") for s in steps) and language:
        add(f"language:{language[:2]}", f"Language · {LANGUAGE_NAMES.get(language[:2], language)}")

    if steps:
        live = not any(s["id"].startswith("submit:") for s in steps)
        if live:
            steps[-1]["state"] = "current"
    return steps


def derive_intents(
    tool_calls: list[dict[str, Any]],
    submissions: list[dict[str, Any]],
    patient: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """One row per closing action, plus an open one if tools ran with no submit."""
    by_action: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def ensure(action: str, title: str) -> dict[str, Any]:
        if action not in by_action:
            by_action[action] = {
                "id": action,
                "title": title,
                "state": "active",
                "steps": [],
                "patient": _patient_name(patient) if patient else None,
            }
            order.append(action)
        return by_action[action]

    for call in tool_calls:
        name = call.get("name") or ""
        result = call.get("result") or {}
        if name in ("lookup_patient", "open_chart"):
            row = ensure("book", "Book an appointment")
            row["steps"].append("patient identified" if name == "open_chart" else "looking up")
        elif name == "find_appointments":
            row = ensure("book", "Book an appointment")
            row["steps"].append("selecting appointment")
            if result.get("asked_for"):
                row["detail"] = result["asked_for"]
        elif name == "book_slot":
            ensure("book", "Book an appointment")["steps"].append("confirming")
        elif name == "cancel_appointment":
            ensure("cancel", "Cancel an appointment")["steps"].append("cancelling")
        elif name == "reschedule_appointment":
            ensure("reschedule", "Reschedule an appointment")["steps"].append("rescheduling")
        elif name == "register_new_patient":
            ensure("register", "Register a new patient")["steps"].append("collecting details")
        elif name == "end_without_booking":
            ensure("no_action", "Close without booking")["steps"].append("closing")
        elif name == "escalate_call":
            ensure("escalate", "Escalate")["steps"].append("escalating")
        elif name == "check_symptom" and result.get("escalate"):
            row = ensure("escalate", "Escalate")
            row["steps"].append("red flag")

    for submit in submissions:
        action = submit.get("action") or "unknown"
        title = {
            "book": "Book an appointment",
            "cancel": "Cancel an appointment",
            "reschedule": "Reschedule an appointment",
            "register": "Register a new patient",
            "no_action": "Close without booking",
            "escalate": "Escalate",
        }.get(action, action)
        row = ensure(action, title)
        ok = submit.get("accepted") or submit.get("dry_run")
        row["state"] = "done" if ok else "recorded"
        row["status"] = submit.get("status")
        payload = submit.get("payload") or {}
        if payload.get("slot"):
            row["detail"] = payload["slot"]
        if payload.get("reason"):
            row["detail"] = payload["reason"]

    return [by_action[key] for key in order]


def derive_why(
    tool_calls: list[dict[str, Any]],
    submissions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    patient: dict[str, Any] | None,
) -> dict[str, Any]:
    """Deterministic explanation of the last booking-shaped decision."""
    search = _last_tool(tool_calls, "find_appointments")
    book = _last_tool(tool_calls, "book_slot")
    nearest = _last_tool(tool_calls, "nearest_site")
    booked = next((s for s in reversed(submissions) if s.get("action") == "book"), None)
    facts: list[dict[str, str]] = []

    if patient:
        facts.append({"label": "Patient", "value": _patient_name(patient) or "identified"})
        if patient.get("insurer"):
            facts.append({"label": "Insurance", "value": str(patient["insurer"])})

    result = (search or {}).get("result") or {}
    args = (search or {}).get("arguments") or {}
    requested = result.get("asked_for") or args.get("when")
    if requested:
        facts.append({"label": "Patient requested", "value": str(requested)})
    if args.get("provider_name") or args.get("doctor"):
        facts.append({"label": "Doctor requested", "value": str(args.get("provider_name") or args.get("doctor"))})
    if nearest:
        serving = (nearest.get("result") or {}).get("nearest_serving") or {}
        if serving.get("name") or serving.get("location_id"):
            facts.append({
                "label": "Nearest site",
                "value": serving.get("name") or serving.get("location_id"),
            })

    notes: list[Any] = []
    for call in tool_calls:
        if call.get("name") != "find_appointments":
            continue
        extra = (call.get("result") or {}).get("notes") or []
        if isinstance(extra, list):
            notes.extend(extra)
    if isinstance(notes, list):
        for note in notes:
            text = str(note)
            if any(token in text.lower() for token in _INSURANCE):
                facts.append({"label": "Constraint", "value": text})
            elif "leave" in text.lower() or "does not" in text.lower():
                facts.append({"label": "Constraint", "value": text})

    if result.get("reason"):
        facts.append({"label": "Rule", "value": str(result["reason"])})

    confirmed = ((book or {}).get("result") or {}).get("confirmed") or {}
    payload = (booked or {}).get("payload") or {}
    if confirmed.get("doctor") or payload.get("provider_id"):
        facts.append({
            "label": "Booked with",
            "value": confirmed.get("doctor") or payload.get("provider_id"),
        })
    if confirmed.get("when") or payload.get("slot"):
        facts.append({
            "label": "Availability",
            "value": confirmed.get("when") or payload.get("slot"),
        })
    if confirmed.get("site") or payload.get("location_id"):
        facts.append({
            "label": "Site",
            "value": confirmed.get("site") or payload.get("location_id"),
        })

    why_line = None
    for decision in reversed(decisions):
        if decision.get("why"):
            why_line = str(decision["why"])
            break
        if decision.get("stage") == "booked":
            why_line = "Caller accepted a valid option returned by the clinic."
            break
        if decision.get("stage") in ("cancelled", "rescheduled", "registered"):
            why_line = f"Recorded {decision['stage']}."
            break

    if not facts and not why_line:
        return {
            "headline": "No additional decision trace available.",
            "facts": [],
            "decision": None,
        }
    return {
        "headline": "Why this decision",
        "facts": facts,
        "decision": why_line or "The tools and the clinic record are the source; nothing was invented.",
    }


def derive_safety(
    tool_calls: list[dict[str, Any]],
    submissions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    patient: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Visible shields that fired. Empty means none fired — not a failure."""
    cards: list[dict[str, Any]] = []
    for call in tool_calls:
        name = call.get("name") or ""
        result = call.get("result") or {}
        blob = f"{result}".lower()
        if name == "check_symptom" and (result.get("escalate") or result.get("red_flag")):
            cards.append({
                "kind": "medical",
                "title": "Medical safety",
                "body": "Red flag detected. Booking stopped. Emergency escalation required.",
            })
        if name == "open_chart" and any(token in blob for token in ("wrong", "not this patient")):
            cards.append({
                "kind": "privacy",
                "title": "Privacy protected",
                "body": "Attempt to open another chart was rejected.",
            })
        if result.get("reason") and any(token in str(result.get("reason")).lower() for token in _REFERRAL):
            cards.append({
                "kind": "clinic",
                "title": "Clinic rule",
                "body": str(result.get("reason")),
            })
        notes = result.get("notes") or []
        if isinstance(notes, list):
            for note in notes:
                text = str(note)
                if any(token in text.lower() for token in ("does not take", "does not cover")):
                    cards.append({
                        "kind": "insurance",
                        "title": "Insurance",
                        "body": text,
                    })
        guidance = str(result.get("guidance") or "").lower()
        if "national id" in guidance or "never read" in guidance:
            cards.append({
                "kind": "privacy",
                "title": "Privacy protected",
                "body": "National id and phone stay off the line unless the caller said them.",
            })

    for submit in submissions:
        if submit.get("action") == "escalate":
            reason = (submit.get("payload") or {}).get("reason") or "escalated"
            cards.append({
                "kind": "medical",
                "title": "Medical safety",
                "body": f"Escalation recorded ({reason}).",
            })
        reason = (submit.get("payload") or {}).get("reason") or ""
        if reason:
            kind = "clinic"
            title = "Clinic rule"
            if "insurance" in reason or "plan" in reason:
                kind, title = "insurance", "Insurance"
            if "privacy" in reason or "third" in reason:
                kind, title = "privacy", "Privacy protected"
            if reason in ("medical_emergency",) or "emergency" in reason:
                kind, title = "medical", "Medical safety"
            cards.append({"kind": kind, "title": title, "body": reason.replace("_", " ")})

    # Deduplicate identical bodies, keep order.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        key = f"{card['kind']}:{card['body']}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(card)
    return unique


def derive_patient_context(
    patient: dict[str, Any] | None,
    chart: dict[str, Any] | None,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    person = dict(patient or {})
    if not person:
        for event in reversed(events):
            if event.get("kind") == "patient_identified":
                person = dict((event.get("payload") or {}).get("patient") or {})
                chart = chart or {
                    "visit_count": (event.get("payload") or {}).get("visit_count"),
                }
                break
    chart = chart or {}
    visits = chart.get("visit_count")
    last = chart.get("last_visit") or {}
    upcoming = (chart.get("upcoming") or [None])[0] or {}
    note = person.get("note") or ""
    hearing = any(token in note.lower() for token in _HEARING) if note else False
    name = _patient_name(person)
    regular = bool(visits and visits >= 3)
    return {
        "name": name,
        "patient_id": person.get("patient_id"),
        "national_id": person.get("national_id"),
        "phone": person.get("phone"),
        "email": person.get("email"),
        "date_of_birth": person.get("date_of_birth"),
        "insurer": person.get("insurer"),
        "note": note or None,
        "visit_count": visits,
        "regular": regular,
        "last_visit": last or None,
        "upcoming": upcoming or None,
        "hearing_support": hearing,
        "known": bool(person.get("has_visited_before") or visits),
    }


def derive_resolution(
    *,
    patient: dict[str, Any] | None,
    language: str,
    languages_seen: list[str],
    intents: list[dict[str, Any]],
    submissions: list[dict[str, Any]],
    why: dict[str, Any],
    metrics: dict[str, Any],
    duration_s: float,
    status: str,
    followups: list[dict[str, Any]],
    from_number: str | None,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    outcomes = []
    for submit in submissions:
        payload = submit.get("payload") or {}
        outcomes.append({
            "action": submit.get("action"),
            "accepted": bool(submit.get("accepted") or submit.get("dry_run")),
            "status": submit.get("status"),
            "dry_run": bool(submit.get("dry_run")),
            "when": payload.get("slot"),
            "provider_id": payload.get("provider_id"),
            "location_id": payload.get("location_id"),
            "reason": payload.get("reason"),
        })
    lang_line = " → ".join(LANGUAGE_NAMES.get(c, c) for c in languages_seen) or LANGUAGE_NAMES.get(language[:2], language)
    follow = followups[-1] if followups else None
    closed = status not in ("live", "ringing") or bool(submissions)
    return {
        "closed": closed and bool(submissions or status in ("finished", "rehearsed", "failed")),
        "patient": _patient_name(patient),
        "from_number": from_number,
        "language": lang_line,
        "intents": [row["title"] for row in intents],
        "outcomes": outcomes,
        "interruptions": metrics.get("interruptions") or 0,
        "duration_s": duration_s,
        "followup": follow,
        "errors": len(errors),
        "decision": why.get("decision"),
    }


def compact_reliability(stats: dict[str, Any], extras: dict[str, Any] | None = None) -> dict[str, Any]:
    """Labels the jury can read. HTTP 200 is an accepted record, not a scored pass."""
    extras = extras or {}
    return {
        "live_calls": stats.get("live") or 0,
        "accepted_records": stats.get("with_accepted_submission") or 0,
        "calls_with_errors": stats.get("calls_with_errors") or 0,
        "median_response_ms": stats.get("median_response_ms"),
        "p90_response_ms": stats.get("p90_response_ms"),
        "peak_concurrency": stats.get("peak_concurrency") or 0,
        "interruptions_handled": stats.get("interruptions") or 0,
        "median_llm_ms": stats.get("median_llm_ms"),
        "median_tts_ms": stats.get("median_tts_first_byte_ms"),
        "languages": extras.get("languages") or {},
        "average_duration_s": extras.get("average_duration_s"),
        "stt_errors": extras.get("stt_errors") or 0,
        "llm_errors": extras.get("llm_errors") or 0,
        "tts_errors": extras.get("tts_errors") or 0,
        "safety_escalations": extras.get("safety_escalations") or 0,
    }


def hearing_support_from_note(note: Optional[str]) -> bool:
    text = (note or "").lower()
    return any(token in text for token in _HEARING)


def _patient_name(patient: Optional[dict[str, Any]]) -> Optional[str]:
    if not patient:
        return None
    if patient.get("full_name"):
        return str(patient["full_name"])
    parts = [patient.get(k) for k in ("given_name", "first_surname", "second_surname")]
    name = " ".join(str(p) for p in parts if p).strip()
    return name or None


def _last_tool(tool_calls: list[dict[str, Any]], name: str) -> Optional[dict[str, Any]]:
    for call in reversed(tool_calls):
        if call.get("name") == name:
            return call
    return None


def overlay_from_call(call: Any) -> dict[str, Any]:
    """LiveCall or anything with the same fields."""
    chart = getattr(call, "chart", None)
    followups = list(getattr(call, "followup_emails", None) or getattr(call, "followups", []) or [])
    return overlay(
        events=list(getattr(call, "events", []) or []),
        tool_calls=list(getattr(call, "tool_calls", []) or []),
        submissions=list(getattr(call, "submissions", []) or []),
        decisions=list(getattr(call, "decisions", []) or []),
        patient=getattr(call, "patient", None),
        chart=chart,
        language=getattr(call, "language", "es") or "es",
        metrics=getattr(call, "metrics", None),
        status=getattr(call, "status", "live") or "live",
        duration_s=getattr(call, "duration_s", 0.0) or 0.0,
        from_number=getattr(call, "from_number", None),
        errors=list(getattr(call, "errors", []) or []),
        followups=followups,
    )


def overlay_from_events(events: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    tool_calls = [e.get("payload") or {} for e in events if e.get("kind") == "tool_call"]
    submissions = [e.get("payload") or {} for e in events if e.get("kind") == "submit"]
    decisions = [e.get("payload") or {} for e in events if e.get("kind") == "decision"]
    followups = [e.get("payload") or {} for e in events if e.get("kind") == "followup_email"]
    patient = None
    chart = None
    language = kwargs.get("language") or "es"
    for event in events:
        payload = event.get("payload") or {}
        if event.get("kind") == "patient_identified":
            patient = payload.get("patient")
            chart = {"visit_count": payload.get("visit_count")}
        if event.get("kind") == "tool_call" and payload.get("name") == "open_chart":
            chart = payload.get("result") or chart
        if payload.get("language"):
            language = payload["language"]
    return overlay(
        events=events,
        tool_calls=tool_calls,
        submissions=submissions,
        decisions=decisions,
        patient=patient,
        chart=chart,
        language=language,
        followups=followups,
        **{k: v for k, v in kwargs.items() if k != "language"},
    )


def _languages_seen(events: list[dict[str, Any]], fallback: str) -> list[str]:
    seen: list[str] = []
    for event in events:
        payload = event.get("payload") or {}
        code = payload.get("language")
        if event.get("kind") in ("language_detected", "stt_final") and code:
            code = str(code)[:2]
            if code and (not seen or seen[-1] != code):
                seen.append(code)
    if not seen and fallback:
        seen.append(fallback[:2])
    return seen
