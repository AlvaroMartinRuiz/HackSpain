"""Patients this clinic process has already recognised.

Prosper remains the live directory and the source of slots. Talk dry-runs never
write there, so a second local call would otherwise not recognise someone we
just registered. This file is the desk's memory for those calls, and a cache of
people Prosper already confirmed.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Optional

from src.config import DATA_DIR
from src.domain.identity import normalize_phone, normalize_text, parse_national_id

PATH = DATA_DIR / "known_patients.json"
_LOCK = threading.Lock()


def remember(person: dict[str, Any]) -> dict[str, Any]:
    """Keep one chart locally so the next call can find them."""
    record = _record(person)
    if not record:
        return {}
    with _LOCK:
        people = _load()
        people = [row for row in people if not _same(row, record)]
        people.append(record)
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(people, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def search(
    name: Optional[str] = None,
    national_id: Optional[str] = None,
    phone: Optional[str] = None,
    date_of_birth: Optional[str] = None,
) -> list[dict[str, Any]]:
    if not (name or national_id or phone or date_of_birth):
        return []
    id_value = ""
    if national_id:
        parsed = parse_national_id(national_id)
        id_value = str(parsed.get("value") or "")
    phone_value = normalize_phone(phone) if phone else ""
    name_tokens = [token for token in normalize_text(name).split() if token] if name else []
    hits: list[dict[str, Any]] = []
    with _LOCK:
        people = _load()
    for row in people:
        if id_value and str(row.get("national_id") or "") == id_value:
            hits.append(row)
            continue
        if phone_value and normalize_phone(str(row.get("phone") or "")) == phone_value:
            hits.append(row)
            continue
        if date_of_birth and str(row.get("date_of_birth") or "") != date_of_birth:
            continue
        haystack = normalize_text(str(row.get("full_name") or ""))
        if name_tokens and all(token in haystack for token in name_tokens):
            hits.append(row)
            continue
        if date_of_birth and str(row.get("date_of_birth") or "") == date_of_birth and not name_tokens:
            hits.append(row)
    return hits


def chart(patient_id: str) -> dict[str, Any] | None:
    with _LOCK:
        for row in _load():
            if row.get("patient_id") == patient_id:
                return row
    return None


def _load() -> list[dict[str, Any]]:
    if not PATH.exists():
        return []
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in data if isinstance(row, dict)]


def _same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("patient_id") and left.get("patient_id") == right.get("patient_id"):
        return True
    if left.get("national_id") and left.get("national_id") == right.get("national_id"):
        return True
    if left.get("phone") and normalize_phone(str(left.get("phone"))) == normalize_phone(str(right.get("phone") or "")):
        return True
    return False


def _record(person: dict[str, Any]) -> dict[str, Any]:
    given = str(person.get("given_name") or "").strip()
    first = str(person.get("first_surname") or "").strip()
    second = str(person.get("second_surname") or "").strip()
    full = str(person.get("full_name") or "").strip() or " ".join(part for part in (given, first, second) if part)
    national_id = str(person.get("national_id") or "").strip()
    parsed = parse_national_id(national_id) if national_id else {}
    if parsed.get("valid") or parsed.get("letter_missing"):
        national_id = str(parsed.get("value") or national_id)
    phone = normalize_phone(str(person.get("phone") or ""))
    patient_id = str(person.get("patient_id") or "").strip() or (f"local-{national_id or phone}" if (national_id or phone) else "")
    if not full and not patient_id:
        return {}
    return {
        "patient_id": patient_id,
        "given_name": given or (full.split()[0] if full else ""),
        "first_surname": first,
        "second_surname": second,
        "full_name": full,
        "national_id": national_id,
        "phone": phone,
        "date_of_birth": str(person.get("date_of_birth") or "").strip(),
        "email": str(person.get("email") or "").strip(),
        "insurer": str(person.get("insurer") or "").strip(),
        "has_visited_before": True,
        "note": str(person.get("note") or "").strip(),
    }
