"""The clinic catalogue, loaded once and held.

Generated once for the whole event and identical for every team and every
call, so it is cached on disk and read from memory afterwards.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Optional

from v2.config import Config
from v2.domain.geo import haversine_km
from v2.domain.identity import normalize_provider_name, normalize_text

WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# The universal pair, used by any specialty without types of its own.
UNIVERSAL_TYPES = {"first_visit", "review"}


@dataclass(frozen=True)
class Location:
    id: str
    name: str
    address: str
    latitude: float
    longitude: float
    hours: dict[str, list[tuple[time, time]]]

    def is_open_on(self, day: date) -> bool:
        return bool(self.hours.get(WEEKDAY_NAMES[day.weekday()]))


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    specialty_id: str
    languages: tuple[str, ...]
    location_ids: tuple[str, ...]
    refused_insurers: tuple[str, ...]
    leave_start: Optional[date]
    leave_end: Optional[date]

    def on_leave_on(self, day: date) -> bool:
        if self.leave_start is None or self.leave_end is None:
            return False
        return self.leave_start <= day <= self.leave_end

    def speaks(self, language: str) -> bool:
        return language.lower()[:2] in self.languages


@dataclass(frozen=True)
class Specialty:
    id: str
    name: str
    min_age_months: int
    max_age_months: Optional[int]
    referral_required: bool
    not_covered_by: tuple[str, ...]

    def accepts_age_months(self, months: int) -> bool:
        if months < self.min_age_months:
            return False
        return self.max_age_months is None or months <= self.max_age_months


@dataclass(frozen=True)
class AppointmentType:
    id: str
    name: str
    specialty_id: Optional[str]
    duration_minutes: int
    new_patient_requirement: str
    guidance: str

    @property
    def for_new_patient(self) -> bool:
        return self.new_patient_requirement == "new_only"


class Catalog:
    def __init__(self, raw: dict[str, Any]) -> None:
        clinic = raw.get("clinic", raw)
        self.raw = clinic
        self.clinic_name: str = clinic.get("clinic_name", "Clínica Arenal")

        calendar = clinic.get("calendar", {})
        self.calendar_start = _as_date(calendar.get("starts"))
        self.calendar_end = _as_date(calendar.get("ends"))
        self.max_span_days: int = calendar.get("max_span_days", 14)
        self.slot_minutes: int = calendar.get("slot_minutes", 15)
        self.closure_days: set[date] = {
            d for d in (_as_date(x) for x in calendar.get("closure_days", [])) if d
        }

        self.locations: dict[str, Location] = {}
        for item in clinic.get("locations", []):
            self.locations[item["id"]] = Location(
                id=item["id"],
                name=item["name"],
                address=item["address"],
                latitude=item["latitude"],
                longitude=item["longitude"],
                hours=_parse_hours(item.get("hours", [])),
            )

        self.providers: dict[str, Provider] = {}
        for item in clinic.get("providers", []):
            leave = item.get("leave") or {}
            self.providers[item["id"]] = Provider(
                id=item["id"],
                name=item["name"],
                specialty_id=item["specialty_id"],
                languages=tuple(item.get("languages", [])),
                location_ids=tuple(s["location_id"] for s in item.get("schedules", [])),
                refused_insurers=tuple(i["id"] for i in item.get("refused_insurers", [])),
                leave_start=_as_date(leave.get("start")),
                leave_end=_as_date(leave.get("end")),
            )

        self.specialties: dict[str, Specialty] = {}
        for item in clinic.get("specialties", []):
            self.specialties[item["id"]] = Specialty(
                id=item["id"],
                name=item["name"],
                min_age_months=item["min_age_months"],
                max_age_months=item.get("max_age_months"),
                referral_required=item.get("referral_required", False),
                not_covered_by=tuple(i["id"] for i in item.get("not_covered_by", [])),
            )

        self.appointment_types: dict[str, AppointmentType] = {}
        for item in clinic.get("appointment_types", []):
            self.appointment_types[item["id"]] = AppointmentType(
                id=item["id"],
                name=item["name"],
                specialty_id=item.get("specialty_id"),
                duration_minutes=item["duration_minutes"],
                new_patient_requirement=item["new_patient_requirement"],
                guidance=item.get("guidance", ""),
            )

        self.plans: dict[str, dict[str, Any]] = {p["id"]: p for p in clinic.get("plans", [])}
        self.restrictions: dict[str, dict[str, Any]] = {
            r["id"]: r for r in clinic.get("restrictions", [])
        }

        self._provider_by_norm_name: dict[str, list[Provider]] = {}
        for provider in self.providers.values():
            key = normalize_provider_name(provider.name)
            self._provider_by_norm_name.setdefault(key, []).append(provider)

    # ---- loading -----------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Catalog":
        target = path or Config().catalog_path
        raw = json.loads(target.read_text(encoding="utf-8"))
        return cls(raw)

    # ---- calendar ----------------------------------------------------

    def is_closure_day(self, day: date) -> bool:
        return day in self.closure_days

    def is_open(self, day: date, location_id: Optional[str] = None) -> bool:
        if self.is_closure_day(day):
            return False
        if location_id:
            location = self.locations.get(location_id)
            return bool(location and location.is_open_on(day))
        return any(loc.is_open_on(day) for loc in self.locations.values())

    def next_open_day(self, day: date, location_id: Optional[str] = None, limit: int = 30) -> Optional[date]:
        from datetime import timedelta

        candidate = day
        for _ in range(limit):
            if self.in_calendar(candidate) and self.is_open(candidate, location_id):
                return candidate
            candidate = candidate + timedelta(days=1)
        return None

    def in_calendar(self, day: date) -> bool:
        if self.calendar_start and day < self.calendar_start:
            return False
        if self.calendar_end and day > self.calendar_end:
            return False
        return True

    def clamp_to_calendar(self, day: date) -> date:
        if self.calendar_start and day < self.calendar_start:
            return self.calendar_start
        if self.calendar_end and day > self.calendar_end:
            return self.calendar_end
        return day

    # ---- providers ---------------------------------------------------

    def find_providers_by_name(self, spoken: str) -> list[Provider]:
        """Candidates for a name said out loud.

        A surname a phone line cannot separate from another — Sáez and Sáenz,
        Iglesias and Iglesia — returns both, because which one the caller means
        is a question, not an inference.
        """
        query = normalize_provider_name(spoken)
        if not query:
            return []

        query_tokens = [token for token in query.split(" ") if len(token) > 1]
        if not query_tokens:
            return []

        scored: list[tuple[int, Provider]] = []
        for provider in self.providers.values():
            tokens = normalize_provider_name(provider.name).split(" ")
            exact = sum(1 for token in query_tokens if token in tokens)
            near = sum(
                1 for token in query_tokens
                for other in tokens
                if token not in tokens and _near_miss(token, other)
            )
            if exact or near:
                # An exact surname outranks a near miss, but both stay on the table.
                scored.append((exact * 2 + min(near, 1), provider))

        if not scored:
            return []
        best = max(score for score, _ in scored)
        # Keep near misses of the best match so the ambiguity is visible.
        keep = [provider for score, provider in scored if score >= max(1, best - 1)]
        keep.sort(key=lambda p: -next(s for s, q in scored if q is p))
        return keep

    def providers_for_specialty(self, specialty_id: str) -> list[Provider]:
        return [p for p in self.providers.values() if p.specialty_id == specialty_id]

    # ---- appointment types -------------------------------------------

    def expected_appointment_type(self, specialty_id: str, has_visited_before: bool) -> Optional[AppointmentType]:
        """The one type that fits a specialty and a patient's history.

        A specialty's own types win over the universal pair; where it has only
        one of the two, the universal type fills the gap.
        """
        want_new = not has_visited_before
        own = [
            t for t in self.appointment_types.values()
            if t.specialty_id == specialty_id and t.for_new_patient == want_new
        ]
        if own:
            return own[0]
        fallback = "first_visit" if want_new else "review"
        return self.appointment_types.get(fallback)

    # ---- plans -------------------------------------------------------

    def plan_covers_specialty(self, plan_id: str, specialty_id: str) -> bool:
        specialty = self.specialties.get(specialty_id)
        return bool(specialty and plan_id not in specialty.not_covered_by)

    def plan_covers_location(self, plan_id: str, location_id: str) -> bool:
        plan = self.plans.get(plan_id)
        location = self.locations.get(location_id)
        if not plan or not location:
            return True
        return location.name not in plan.get("uncovered_location_names", [])

    # ---- sites -------------------------------------------------------

    def rank_locations_by_distance(self, latitude: float, longitude: float) -> list[tuple[Location, float]]:
        ranked = [
            (location, haversine_km(latitude, longitude, location.latitude, location.longitude))
            for location in self.locations.values()
        ]
        ranked.sort(key=lambda pair: pair[1])
        return ranked

    def locations_serving(self, specialty_id: str) -> set[str]:
        serving: set[str] = set()
        for provider in self.providers.values():
            if provider.specialty_id == specialty_id:
                serving.update(provider.location_ids)
        return serving

    # ---- misc --------------------------------------------------------

    def specialty_for_age(self, age_months: int, general: bool = True) -> Optional[str]:
        """Every age has exactly one correct specialty for a general complaint."""
        for specialty_id in ("paediatrics", "general_practice"):
            specialty = self.specialties.get(specialty_id)
            if specialty and specialty.accepts_age_months(age_months):
                return specialty_id
        return "general_practice" if general else None

    def summary(self) -> dict[str, Any]:
        return {
            "clinic_name": self.clinic_name,
            "locations": [
                {"id": loc.id, "name": loc.name, "address": loc.address,
                 "latitude": loc.latitude, "longitude": loc.longitude,
                 "open_days": sorted(loc.hours.keys(), key=WEEKDAY_NAMES.index)}
                for loc in self.locations.values()
            ],
            "providers": [
                {"id": p.id, "name": p.name, "specialty_id": p.specialty_id,
                 "languages": list(p.languages), "locations": list(p.location_ids),
                 "refused_insurers": list(p.refused_insurers),
                 "on_leave": [p.leave_start.isoformat(), p.leave_end.isoformat()]
                 if p.leave_start and p.leave_end else None}
                for p in self.providers.values()
            ],
            "specialties": [
                {"id": s.id, "name": s.name, "min_age_months": s.min_age_months,
                 "max_age_months": s.max_age_months, "referral_required": s.referral_required,
                 "not_covered_by": list(s.not_covered_by)}
                for s in self.specialties.values()
            ],
            "appointment_types": [
                {"id": t.id, "specialty_id": t.specialty_id,
                 "new_patient_requirement": t.new_patient_requirement,
                 "duration_minutes": t.duration_minutes}
                for t in self.appointment_types.values()
            ],
            "calendar": {
                "starts": self.calendar_start.isoformat() if self.calendar_start else None,
                "ends": self.calendar_end.isoformat() if self.calendar_end else None,
                "closure_days": sorted(d.isoformat() for d in self.closure_days),
                "max_span_days": self.max_span_days,
            },
        }


def age_months(date_of_birth: date, on: date) -> int:
    months = (on.year - date_of_birth.year) * 12 + (on.month - date_of_birth.month)
    if on.day < date_of_birth.day:
        months -= 1
    return max(months, 0)


def _as_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_hours(entries: Iterable[dict[str, Any]]) -> dict[str, list[tuple[time, time]]]:
    hours: dict[str, list[tuple[time, time]]] = {}
    for entry in entries:
        weekday = normalize_text(entry.get("weekday", ""))
        spans: list[tuple[time, time]] = []
        for interval in entry.get("intervals", []):
            # Intervals arrive as "08:00–20:00", with an en dash.
            parts = re.split(r"[–\-—]", interval)
            if len(parts) != 2:
                continue
            try:
                start = time.fromisoformat(parts[0].strip())
                end = time.fromisoformat(parts[1].strip())
            except ValueError:
                continue
            spans.append((start, end))
        if spans:
            hours[weekday] = spans
    return hours


def _near_miss(a: str, b: str) -> bool:
    """True for surnames a phone line cannot tell apart: Sáez / Sáenz."""
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 4:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) == len(longer):
        return sum(1 for x, y in zip(shorter, longer) if x != y) == 1
    for index in range(len(longer)):
        if longer[:index] + longer[index + 1 :] == shorter:
            return True
    return False
