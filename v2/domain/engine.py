"""The deterministic core: what may be booked, when, and under which plan.

The model decides what the caller wants. Everything that ends up in a record —
ids, the exact minute, the appointment type, the plan, the refusal reason — is
decided here, from what the clinic actually returned.
"""

from __future__ import annotations

from difflib import get_close_matches
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence

from v2.domain import gazetteer
from v2.domain.catalog import Catalog, age_months
from v2.domain.identity import (
    normalize_email,
    normalize_phone,
    normalize_text,
    parse_national_id,
    peel_insurer_from_email,
    split_id_and_phone,
)
from v2.domain.outcomes import pick_blocking_reason
from v2.domain.timeref import (
    MADRID,
    WhenSpec,
    format_slot,
    matches_part_of_day,
    now_madrid,
    parse_slot,
    resolve_when,
)
from v2.platform_api.client import PlatformClient, PlatformError


@dataclass
class Slot:
    provider_id: str
    provider_name: str
    specialty_id: str
    location_id: str
    appointment_type_id: str
    start: datetime
    duration_minutes: int
    payable_with: tuple[str, ...]

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Slot":
        return cls(
            provider_id=raw["provider_id"],
            provider_name=raw["provider_name"],
            specialty_id=raw["specialty_id"],
            location_id=raw["location_id"],
            appointment_type_id=raw["appointment_type_id"],
            start=parse_slot(raw["start_time"]),
            duration_minutes=raw["duration_minutes"],
            payable_with=tuple(raw.get("payable_with", [])),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "specialty_id": self.specialty_id,
            "location_id": self.location_id,
            "appointment_type_id": self.appointment_type_id,
            "slot": format_slot(self.start),
            "duration_minutes": self.duration_minutes,
            "payable_with": list(self.payable_with),
        }


@dataclass
class SlotSearch:
    """What a search found, and why it found nothing when it did."""

    slots: list[Slot] = field(default_factory=list)
    appointment_type_id: Optional[str] = None
    blocked: list[dict[str, str]] = field(default_factory=list)
    reason: Optional[str] = None
    window: tuple[Optional[date], Optional[date]] = (None, None)
    when: Optional[WhenSpec] = None
    trace: list[str] = field(default_factory=list)
    exact_day_met: bool = True
    part_of_day_possible: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.slots)

    def as_dict(self, limit: int = 5) -> dict[str, Any]:
        return {
            "found": self.found,
            "appointment_type_id": self.appointment_type_id,
            "reason": self.reason,
            "window": [d.isoformat() if d else None for d in self.window],
            "asked_for": self.when.describe() if self.when else None,
            "exact_day_met": self.exact_day_met,
            "part_of_day_possible": self.part_of_day_possible,
            "notes": self.notes,
            "blocked": self.blocked,
            "slots": [slot.as_dict() for slot in self.slots[:limit]],
            "trace": self.trace,
        }


@dataclass
class BookingPlan:
    """A validated payload, ready to submit exactly as it stands."""

    patient_id: str
    provider_id: str
    location_id: str
    appointment_type_id: str
    slot: str
    policy_id: str
    provider_name: str
    warnings: list[str] = field(default_factory=list)

    def payload(self, call_id: str) -> dict[str, Any]:
        return {
            "call_id": call_id,
            "patient_id": self.patient_id,
            "provider_id": self.provider_id,
            "location_id": self.location_id,
            "appointment_type_id": self.appointment_type_id,
            "slot": self.slot,
            "policy_id": self.policy_id,
        }


class SchedulingEngine:
    def __init__(self, client: PlatformClient, catalog: Catalog) -> None:
        self.client = client
        self.catalog = catalog

    # ---- identification ----------------------------------------------

    async def identify(
        self,
        name: Optional[str] = None,
        national_id: Optional[str] = None,
        phone: Optional[str] = None,
        date_of_birth: Optional[str] = None,
    ) -> dict[str, Any]:
        """Look a caller up, and say plainly what the directory answered.

        An exact field that does not match excludes a patient rather than
        ranking them down, so a national id is only sent when its own check
        letter agrees with its digits.
        """
        trace: list[str] = []
        national_id_clean: Optional[str] = None
        if national_id:
            parsed = parse_national_id(national_id)
            if parsed["valid"]:
                national_id_clean = str(parsed["value"])
            elif parsed["letter_missing"]:
                # A misheard digit derives a different, equally valid id, which
                # can be someone else's. On its own it identifies nobody; next
                # to a name or a date of birth the directory's exact filter on
                # both is what rules the wrong person out.
                if not (name or phone or date_of_birth):
                    trace.append(
                        f"national id {national_id!r} arrived without its letter and on its "
                        "own; it needs a second field (name or date of birth) before searching"
                    )
                    return {"count": 0, "matches": [], "ambiguous": False, "trace": trace,
                            "distinguishers": [], "needs_more": True, "letter_inferred": True}
                national_id_clean = str(parsed["value"])
                trace.append(
                    f"national id {national_id!r} arrived without its letter; searched as "
                    f"{national_id_clean} together with the other fields"
                )
            else:
                trace.append(
                    f"national id {national_id!r} failed its check letter "
                    f"(expected {parsed['expected_letter']}); searched without it"
                )

        async def search(**kwargs: Any) -> list[dict[str, Any]]:
            try:
                return await self.client.directory(**kwargs)
            except PlatformError as exc:
                # The directory wants a given name plus a surname, or one exact
                # field. Anything less is a question for the caller.
                if exc.status == 422:
                    trace.append(_needs_more(exc.detail))
                    return []
                raise

        matches = await search(
            name=name,
            national_id=national_id_clean,
            phone=normalize_phone(phone) if phone else None,
            date_of_birth=date_of_birth,
        )

        # A national id that returns nothing is usually misheard; fall back to
        # the softer fields rather than telling the caller they do not exist.
        if not matches and national_id_clean and (name or phone or date_of_birth):
            trace.append("national id matched nobody; retried on name, phone and date of birth")
            matches = await search(
                name=name,
                phone=normalize_phone(phone) if phone else None,
                date_of_birth=date_of_birth,
            )

        return {
            "count": len(matches),
            "matches": [_patient_brief(m) for m in matches[:6]],
            "ambiguous": len(matches) > 1,
            "trace": trace,
            "distinguishers": _distinguishers(matches) if len(matches) > 1 else [],
            "needs_more": any("needs" in line for line in trace),
        }

    async def patient_context(self, patient_id: str) -> dict[str, Any]:
        """The chart as a receptionist would have it open: who, and what history."""
        upcoming = await self.client.appointments(patient_id, when="upcoming")
        past = await self.client.appointments(patient_id, when="past")
        return {
            "patient_id": patient_id,
            "upcoming": [self._describe_appointment(a) for a in upcoming],
            "past": [self._describe_appointment(a) for a in past[-6:]],
            "visit_count": len(past),
            "last_visit": self._describe_appointment(past[-1]) if past else None,
        }

    def _describe_appointment(self, raw: dict[str, Any]) -> dict[str, Any]:
        provider = self.catalog.providers.get(raw.get("provider_id", ""))
        location = self.catalog.locations.get(raw.get("location_id", ""))
        start = parse_slot(raw["start_time"])
        return {
            "appointment_id": raw["appointment_id"],
            "slot": format_slot(start),
            "when": start.strftime("%Y-%m-%d %H:%M"),
            "provider_id": raw.get("provider_id"),
            "provider_name": provider.name if provider else raw.get("provider_id"),
            "specialty_id": provider.specialty_id if provider else None,
            "location_id": raw.get("location_id"),
            "location_name": location.name if location else raw.get("location_id"),
            "appointment_type_id": raw.get("appointment_type_id"),
        }

    # ---- availability -------------------------------------------------

    async def find_slots(
        self,
        patient_id: Optional[str] = None,
        specialty_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        location_id: Optional[str] = None,
        when: Optional[str] = None,
        part_of_day: Optional[str] = None,
        language: Optional[str] = None,
        insurers: Optional[Sequence[str]] = None,
        now: Optional[datetime] = None,
        limit: int = 6,
    ) -> SlotSearch:
        now = now or now_madrid()
        spec = resolve_when(when, now)
        if part_of_day:
            spec.part_of_day = part_of_day

        search = SlotSearch(when=spec)
        search.trace.append(f"asked for: {spec.describe()} (rule: {spec.matched})")

        if provider_id and provider_id in self.catalog.providers:
            specialty_id = specialty_id or self.catalog.providers[provider_id].specialty_id

        windows = self._windows(spec, now, location_id)
        if not windows:
            search.reason = "no_availability"
            search.trace.append("no bookable day left inside the calendar")
            return search

        for date_from, date_to in windows:
            search.window = (date_from, date_to)
            try:
                raw = await self.client.availability(
                    date_from=date_from.isoformat(),
                    date_to=date_to.isoformat(),
                    provider_id=provider_id,
                    specialty_id=None if provider_id else specialty_id,
                    location_id=location_id,
                    patient_id=patient_id,
                    insurer=list(insurers) if insurers else None,
                )
            except Exception as exc:  # a failed read must not end the call silently
                search.trace.append(f"availability failed: {exc}")
                continue

            search.appointment_type_id = (raw.get("appointment_type") or {}).get("id")
            search.blocked = raw.get("blocked", []) or []
            candidates = [Slot.from_api(item) for item in raw.get("slots", [])]
            search.trace.append(
                f"window {date_from}..{date_to}: {len(candidates)} slot(s), "
                f"{len(search.blocked)} blocked"
            )

            candidates = self._apply_floor(candidates, now, search)
            if language:
                candidates = self._filter_language(candidates, language, search)

            self._note_restrictions(search, provider_id, date_from)

            chosen, exact_day_met = self._pick(candidates, spec, search)
            if chosen:
                search.slots = chosen[:limit]
                search.exact_day_met = exact_day_met
                if spec.part_of_day:
                    search.part_of_day_possible = any(
                        matches_part_of_day(slot.start, spec.part_of_day) for slot in candidates
                    )
                    if not search.part_of_day_possible:
                        search.notes.append(
                            f"nothing this specialty offers falls in the {spec.part_of_day} "
                            f"anywhere in the window — say so rather than implying otherwise"
                        )
                return search

        if search.blocked:
            search.reason = pick_blocking_reason(search.blocked)
            search.trace.append(f"refused by rule: {search.reason}")
        else:
            search.reason = "no_availability"
            search.trace.append("calendar is simply full for what was asked")
        return search

    def _windows(
        self, spec: WhenSpec, now: datetime, location_id: Optional[str]
    ) -> list[tuple[date, date]]:
        """The day ranges to ask about, never including the day of the call."""
        span = self.catalog.max_span_days - 1
        floor = now.date() + timedelta(days=1)
        if self.catalog.calendar_start:
            floor = max(floor, self.catalog.calendar_start)
        end = self.catalog.calendar_end or (floor + timedelta(days=span))
        if floor > end:
            return []

        if spec.target_date:
            start = max(spec.target_date, floor)
            # A day the clinic is shut is not an answer; the caller takes the
            # earliest on the next open day that still matches the rest.
            open_day = self.catalog.next_open_day(start, location_id) or start
            start = min(max(open_day, floor), end)
            return [(start, min(start + timedelta(days=span), end))]

        windows: list[tuple[date, date]] = []
        cursor = floor
        while cursor <= end and len(windows) < 3:
            windows.append((cursor, min(cursor + timedelta(days=span), end)))
            cursor = cursor + timedelta(days=span + 1)
        return windows

    def _note_restrictions(
        self, search: SlotSearch, provider_id: Optional[str], from_day: date
    ) -> None:
        """Say out loud which rule narrowed this, even when slots were found.

        A doctor on leave still has free days after it, so a caller who asked
        for him has to hear about the leave rather than just be offered a date
        three weeks out.
        """
        for entry in search.blocked:
            provider = self.catalog.providers.get(entry.get("provider_id", ""))
            restriction = entry.get("restriction", "")
            if provider is None:
                continue
            if restriction == "provider_on_leave" and provider.leave_end:
                search.notes.append(
                    f"{provider.name} is on leave until {provider.leave_end.isoformat()}"
                )
            elif restriction == "provider_not_in_network":
                search.notes.append(f"{provider.name} does not take this patient's plan")
            elif restriction:
                search.notes.append(f"{provider.name}: {restriction}")

        if provider_id:
            provider = self.catalog.providers.get(provider_id)
            if provider and provider.on_leave_on(from_day) and provider.leave_end:
                back = provider.leave_end + timedelta(days=1)
                search.notes.append(
                    f"{provider.name} is away on {from_day.isoformat()} and is not back until "
                    f"{back.isoformat()} — offer another doctor as well as that date"
                )

    def _apply_floor(self, slots: list[Slot], now: datetime, search: SlotSearch) -> list[Slot]:
        """Drop anything on the day of the call: it is never an accepted answer."""
        floor = now.date()
        kept = [slot for slot in slots if slot.start.date() > floor]
        dropped = len(slots) - len(kept)
        if dropped:
            search.trace.append(f"dropped {dropped} same-day slot(s)")
        return kept

    def _filter_language(self, slots: list[Slot], language: str, search: SlotSearch) -> list[Slot]:
        code = language.lower()[:2]
        kept = [
            slot for slot in slots
            if (provider := self.catalog.providers.get(slot.provider_id)) and provider.speaks(code)
        ]
        search.trace.append(f"language {code}: {len(kept)} of {len(slots)} slot(s) qualify")
        return kept

    def _pick(
        self, slots: list[Slot], spec: WhenSpec, search: SlotSearch
    ) -> tuple[list[Slot], bool]:
        """Order what is left by what the caller actually asked for.

        When the exact ask cannot be met, both near misses are offered — the
        right day at the wrong hour, and the right hour on a later day — so the
        caller chooses rather than being quietly given one of them.
        """
        if not slots:
            return [], True

        load = _free_slots_per_provider(slots)

        def ordered(pool: list[Slot]) -> list[Slot]:
            # Earliest first, and where several tie on the minute the quieter
            # diary wins, so one provider is not buried while another sits empty.
            return sorted(
                pool, key=lambda slot: (slot.start, -load.get(slot.provider_id, 0), slot.provider_id)
            )

        target, part = spec.target_date, spec.part_of_day
        on_day = (lambda slot: slot.start.date() == target) if target else (lambda slot: True)
        in_part = (lambda slot: matches_part_of_day(slot.start, part)) if part else (lambda slot: True)

        exact = ordered([slot for slot in slots if on_day(slot) and in_part(slot)])
        if exact:
            if spec.first_thing:
                opening = exact[0].start
                kept = [slot for slot in exact if slot.start == opening]
                search.trace.append(
                    f"first thing: kept {len(kept)} slot(s) at {opening.strftime('%H:%M')}"
                )
                return kept, True
            return exact, True

        wrong_hour = ordered([slot for slot in slots if on_day(slot)]) if target and part else []
        later_day = ordered([
            slot for slot in slots
            if in_part(slot) and (not target or slot.start.date() > target)
        ]) if part else []

        if wrong_hour or later_day:
            search.trace.append(
                f"nothing on {target} in the {part}; offering the same day at another hour "
                f"({len(wrong_hour)}) and the {part} on a later day ({len(later_day)})"
            )
            combined = ordered(_unique(later_day[:3] + wrong_hour[:3]))
            return combined, bool(wrong_hour)

        fallback = ordered([slot for slot in slots if not target or slot.start.date() >= target])
        if target:
            search.trace.append(f"{target} had nothing; offered the next available instead")
        return fallback or ordered(slots), False

    # ---- booking ------------------------------------------------------

    def plan_booking(
        self,
        patient: dict[str, Any],
        slot: Slot,
        named_insurers: Optional[Sequence[str]] = None,
    ) -> tuple[Optional[BookingPlan], Optional[str]]:
        """Turn a chosen slot into a payload, or say which rule stopped it."""
        policy_id = _choose_policy(slot, patient.get("insurer"), named_insurers)
        if policy_id is None:
            return None, "specialty_not_covered"

        warnings: list[str] = []
        expected = self.catalog.expected_appointment_type(
            slot.specialty_id, bool(patient.get("has_visited_before"))
        )
        if expected and expected.id != slot.appointment_type_id:
            # Availability is the authority; a mismatch is worth seeing, not overriding.
            warnings.append(
                f"availability returned {slot.appointment_type_id}, "
                f"history suggested {expected.id}"
            )

        specialty = self.catalog.specialties.get(slot.specialty_id)
        if specialty and specialty.referral_required:
            referrals = {normalize_text(r) for r in patient.get("referrals", [])}
            if normalize_text(slot.specialty_id) not in referrals:
                return None, "referral_required"

        plan = BookingPlan(
            patient_id=patient["patient_id"],
            provider_id=slot.provider_id,
            location_id=slot.location_id,
            appointment_type_id=slot.appointment_type_id,
            slot=format_slot(slot.start),
            policy_id=policy_id,
            provider_name=slot.provider_name,
            warnings=warnings,
        )
        return plan, None

    # ---- rules a caller runs into -------------------------------------

    def age_appropriate_specialty(
        self, patient: dict[str, Any], specialty_id: Optional[str], now: Optional[datetime] = None
    ) -> Optional[str]:
        """The general specialty the patient's age requires, when a swap is needed.

        A child asking for "the doctor" means paediatrics and an adult means
        general practice; the caller should never be refused over which word the
        model picked. Only this pair is interchangeable — gynaecology for a child
        is a real refusal, not a routing mistake.
        """
        if specialty_id not in ("general_practice", "paediatrics"):
            return None
        now = now or now_madrid()
        born = patient.get("date_of_birth")
        if not born:
            return None
        try:
            months = age_months(date.fromisoformat(str(born)[:10]), now.date())
        except ValueError:
            return None
        correct = self.catalog.specialty_for_age(months)
        return correct if correct and correct != specialty_id else None

    def check_eligibility(
        self, patient: dict[str, Any], specialty_id: str, now: Optional[datetime] = None
    ) -> Optional[str]:
        """The rule that forbids this pairing, before any calendar is opened."""
        now = now or now_madrid()
        specialty = self.catalog.specialties.get(specialty_id)
        if specialty is None:
            return "type_not_offered"

        born = patient.get("date_of_birth")
        if born:
            try:
                months = age_months(date.fromisoformat(str(born)[:10]), now.date())
            except ValueError:
                months = None
            if months is not None and not specialty.accepts_age_months(months):
                return "not_eligible_age"

        if specialty.referral_required:
            referrals = {normalize_text(r) for r in patient.get("referrals", [])}
            if normalize_text(specialty_id) not in referrals:
                return "referral_required"

        insurer = patient.get("insurer")
        if insurer and insurer in specialty.not_covered_by:
            return "specialty_not_covered"
        return None

    def resolve_provider(self, spoken_name: str) -> dict[str, Any]:
        """Who the caller named, including when a phone line cannot tell."""
        candidates = self.catalog.find_providers_by_name(spoken_name)
        return {
            "count": len(candidates),
            "ambiguous": len(candidates) > 1,
            "providers": [
                {
                    "provider_id": p.id,
                    "name": p.name,
                    "specialty_id": p.specialty_id,
                    "locations": list(p.location_ids),
                    "languages": list(p.languages),
                    "on_leave": (
                        {"start": p.leave_start.isoformat(), "end": p.leave_end.isoformat()}
                        if p.leave_start and p.leave_end else None
                    ),
                    "refused_insurers": list(p.refused_insurers),
                }
                for p in candidates
            ],
        }

    def nearest_site(self, where: str, specialty_id: Optional[str] = None) -> dict[str, Any]:
        """The closest site that can actually serve the request."""
        located = gazetteer.locate(where)
        if located is None:
            return {"resolved": False, "where": where,
                    "note": "could not place that address; ask for the town or district"}

        place, latitude, longitude = located
        serving = self.catalog.locations_serving(specialty_id) if specialty_id else None
        ranked = self.catalog.rank_locations_by_distance(latitude, longitude)
        options = [
            {
                "location_id": location.id,
                "name": location.name,
                "distance_km": round(distance, 2),
                "serves_request": serving is None or location.id in serving,
            }
            for location, distance in ranked
        ]
        eligible = [option for option in options if option["serves_request"]]
        return {
            "resolved": True,
            "where": where,
            "matched_place": place,
            "origin": {"latitude": latitude, "longitude": longitude},
            "options": options,
            "nearest_serving": eligible[0] if eligible else None,
        }

    # ---- registration -------------------------------------------------

    def resolve_insurer(self, spoken: Any) -> Optional[str]:
        """Map a spoken plan name to a clinic id, tolerating one STT vowel."""
        key = normalize_text(str(spoken or "")).replace(" ", "_")
        if not key:
            return None
        aliases: dict[str, str] = {}
        for plan_id, plan in self.catalog.plans.items():
            aliases[normalize_text(plan_id).replace(" ", "_")] = plan_id
            name = normalize_text(str(plan.get("name") or "")).replace(" ", "_")
            if name:
                aliases[name] = plan_id
        for stt, plan_id in (
            ("sinitas", "sanitas"),
            ("zinitas", "sanitas"),
            ("cinitas", "sanitas"),
            ("escinitas", "sanitas"),
            ("a_sisa", "asisa"),
            ("a_deslas", "adeslas"),
            ("nueva_mutua_sanitaria", "nueva_mutua"),
        ):
            if plan_id in self.catalog.plans:
                aliases.setdefault(stt, plan_id)
        if key in aliases:
            return aliases[key]
        close = get_close_matches(key, aliases, n=1, cutoff=0.75)
        return aliases[close[0]] if close else None

    def plan_registration(self, fields: dict[str, Any]) -> tuple[Optional[dict[str, Any]], list[str]]:
        """Build a registration payload, and name whatever is still missing."""
        problems: list[str] = []
        national_id, phone_raw = split_id_and_phone(
            str(fields.get("national_id") or ""),
            str(fields.get("phone") or ""),
        )
        parsed = parse_national_id(national_id)
        if not parsed["valid"] and not parsed.get("letter_missing"):
            expected = parsed["expected_letter"]
            problems.append(
                f"national id does not check out; expected letter {expected}"
                if expected else "national id is not a readable DNI or NIE"
            )

        email, glued_insurer = peel_insurer_from_email(normalize_email(str(fields.get("email", ""))))
        if "@" not in email or "." not in email.split("@")[-1]:
            problems.append("email is not a full address")

        phone = normalize_phone(phone_raw)
        if len(phone) != 9:
            problems.append("phone is not nine digits")

        insurer = self.resolve_insurer(fields.get("insurer") or glued_insurer)
        if insurer is None:
            problems.append(f"insurer {fields.get('insurer')!r} is not one of the clinic's plans")

        born = str(fields.get("date_of_birth", ""))[:10]
        try:
            date.fromisoformat(born)
        except ValueError:
            problems.append("date of birth is not an ISO date")

        for key in ("given_name", "first_surname", "second_surname"):
            if not str(fields.get(key, "")).strip():
                problems.append(f"{key} is missing")

        if problems:
            return None, problems

        return {
            "given_name": str(fields["given_name"]).strip(),
            "first_surname": str(fields["first_surname"]).strip(),
            "second_surname": str(fields["second_surname"]).strip(),
            "national_id": str(parsed["value"]),
            "date_of_birth": born,
            "phone": phone,
            "email": email,
            "insurer": insurer,
        }, []


def _choose_policy(
    slot: Slot, own_insurer: Optional[str], named: Optional[Sequence[str]]
) -> Optional[str]:
    """The plan the appointment is billed against, never a guess.

    A slot payable with several plans is not a licence to pick one the caller
    never held: that is how the second-policy control case is failed.
    """
    payable = [plan for plan in slot.payable_with if plan]
    if not payable:
        return None
    payable_set = set(payable)
    if own_insurer and own_insurer in payable_set:
        return own_insurer
    for insurer in named or ():
        if insurer in payable_set:
            return insurer
    # One plan on the slot is the API's decision, not ours.
    unique = list(dict.fromkeys(payable))
    return unique[0] if len(unique) == 1 else None


def _unique(slots: list[Slot]) -> list[Slot]:
    seen: set[tuple[str, str]] = set()
    out: list[Slot] = []
    for slot in slots:
        key = (slot.provider_id, slot.start.isoformat())
        if key not in seen:
            seen.add(key)
            out.append(slot)
    return out


def _free_slots_per_provider(slots: list[Slot]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for slot in slots:
        counts[slot.provider_id] = counts.get(slot.provider_id, 0) + 1
    return counts


def _patient_brief(match: dict[str, Any]) -> dict[str, Any]:
    return {
        "patient_id": match.get("patient_id"),
        "given_name": match.get("given_name"),
        "first_surname": match.get("first_surname"),
        "second_surname": match.get("second_surname"),
        "full_name": " ".join(
            str(match.get(key, "")) for key in ("given_name", "first_surname", "second_surname")
        ).strip(),
        "date_of_birth": match.get("date_of_birth"),
        "phone": match.get("phone"),
        "sex": match.get("sex"),
        "insurer": match.get("insurer"),
        "referrals": match.get("referrals", []),
        "has_visited_before": match.get("has_visited_before"),
        "note": match.get("note"),
        "matched_fields": match.get("matched_fields", []),
    }


def _needs_more(detail: Any) -> str:
    if isinstance(detail, dict) and detail.get("detail"):
        return f"directory needs more: {detail['detail']}"
    return "directory needs a given name plus a surname, or an exact id, phone or date of birth"


def _distinguishers(matches: list[dict[str, Any]]) -> list[str]:
    """Which field actually separates these people, so the agent asks for it."""
    useful: list[str] = []
    for field_name in ("date_of_birth", "national_id", "phone"):
        values = {str(m.get(field_name)) for m in matches}
        if len(values) == len(matches):
            useful.append(field_name)
    return useful or ["date_of_birth"]
