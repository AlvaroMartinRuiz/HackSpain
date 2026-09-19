from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from typing import Any

from src.domain.catalog import Catalog, WEEKDAY_NAMES
from src.domain.engine import SchedulingEngine
from src.domain.identity import normalize_phone, normalize_provider_name, parse_national_id
from src.domain.timeref import matches_part_of_day, parse_slot
from src.domain.outcomes import ALL_REASONS
from src.platform_api.client import PlatformClient, PlatformError
from v2.domain_rules import identity_problems, registration_plan, requested_clock, resolve_request_when, unsupported_time_request
from v2.models import CallState, Identity, Intent, Offer, Operation, RegistrationFields, SchedulingCriteria
from v2.store import RunStore


class ClinicBoundary:
    def __init__(self, client: PlatformClient, catalog: Catalog):
        self.client = client
        self.engine = SchedulingEngine(client, catalog)
        self.catalog = catalog

    async def identify_result(self, identity: Identity, today: date | None = None) -> dict:
        fields = identity.model_dump(exclude_none=True)
        missing, errors = identity_problems(fields, today or date.today())
        if missing or errors:
            return {"patient": None, "status": "invalid" if errors else "partial", "missing_fields": missing,
                    "validation_errors": errors}
        query = dict(fields)
        if query.get("phone"):
            query["phone"] = normalize_phone(query["phone"])
        if query.get("national_id"):
            query["national_id"] = str(parse_national_id(query["national_id"])["value"])
        try:
            matches = await self.client.directory(**query)
        except PlatformError as exc:
            if exc.status != 422:
                raise
            return {"patient": None, "status": "invalid", "missing_fields": ["identity"],
                    "validation_errors": {"identity": "directory_requires_more"}}
        if len(matches) != 1:
            return {"patient": None, "status": "ambiguous" if matches else "not_found",
                    "missing_fields": ["national_id" if "national_id" not in fields else "phone"]}
        patient = dict(matches[0])
        for key in ("date_of_birth", "phone", "national_id"):
            if query.get(key) and patient.get(key) and str(patient[key]) != query[key]:
                return {"patient": None, "status": "not_found", "missing_fields": ["identity"]}
        if not isinstance(patient.get("patient_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", patient["patient_id"]):
            raise ValueError("directory returned an invalid patient reference")
        patient["upcoming"] = (await self.engine.patient_context(patient["patient_id"]))["upcoming"]
        return {"patient": patient, "status": "verified", "missing_fields": []}

    async def identify(self, identity: Identity) -> dict | None:
        return (await self.identify_result(identity))["patient"]

    @staticmethod
    def _criteria(intent: Intent, op: Operation) -> SchedulingCriteria:
        values = intent.criteria.model_dump()
        if op.criteria:
            values.update(op.criteria.model_dump(exclude_unset=True, exclude_none=True))
        for key in ("specialty_id", "when", "doctor_name", "location_id", "appointment_id"):
            value = getattr(op, key)
            if value is not None:
                values[key] = value
        return SchedulingCriteria(**values)

    async def prepare(self, state: CallState, intent: Intent, op: Operation) -> tuple[list[Offer], str | None]:
        intent.missing_fields, intent.validation_errors, intent.choices = [], {}, []
        if intent.action == "register":
            values = intent.registration_fields.model_dump(exclude_none=True)
            for patch in (op.registration, op.registration_fields):
                if patch:
                    values.update(patch.model_dump(exclude_none=True))
            fields, intent.missing_fields, intent.validation_errors = registration_plan(
                self.engine, RegistrationFields(**values), state.reference_time.date())
            if fields is None:
                return [], None
            return [Offer(revision=intent.revision, action="register", payload=fields, display=fields)], None
        if intent.action not in {"book", "cancel", "reschedule"}:
            return [], "out_of_scope" if intent.action == "no_action" else None
        if intent.patient is None:
            intent.missing_fields, intent.validation_errors = identity_problems(intent.identity_inputs, state.reference_time.date())
            if not intent.missing_fields and not intent.validation_errors:
                intent.missing_fields = ["identity"]
            return [], "patient_not_found" if intent.identity_status == "not_found" else None
        patient = intent.patient
        criteria = self._criteria(intent, op)
        if unsupported_time_request(criteria.when) or unsupported_time_request(criteria.appointment_when):
            intent.validation_errors = {"when": "unsupported_window"}
            return [], None
        existing = None
        if intent.action in {"cancel", "reschedule"}:
            appointments = patient.get("upcoming", [])
            matches = list(appointments)
            if criteria.appointment_id:
                matches = [row for row in matches if row.get("appointment_id") == criteria.appointment_id]
            doctor = criteria.appointment_doctor_name or (criteria.doctor_name if intent.action == "cancel" else None)
            if doctor:
                query = normalize_provider_name(doctor)
                matches = [row for row in matches if set(query.split()) <= set(normalize_provider_name(row.get("provider_name", "")).split())]
            site = criteria.appointment_location_id or (criteria.location_id if intent.action == "cancel" else None)
            if site:
                matches = [row for row in matches if row.get("location_id") == site]
            if criteria.specialty_id:
                matches = [row for row in matches if row.get("specialty_id") == criteria.specialty_id]
            when = criteria.appointment_when or (criteria.when if intent.action == "cancel" else None)
            if when:
                spec = resolve_request_when(when, state.reference_time)
                if spec.target_date is None and requested_clock(when) is None:
                    intent.validation_errors = {"appointment_id": "unrecognized_date"}
                    return [], None
                if spec.target_date:
                    matches = [row for row in matches if str(row.get("slot") or row.get("when", ""))[:10] == spec.target_date.isoformat()]
            clock = criteria.appointment_time or requested_clock(when) or (criteria.time_of_day if intent.action == "cancel" else None)
            if clock:
                matches = [row for row in matches if str(row.get("slot") or row.get("when", ""))[11:16] == clock]
            if len(matches) != 1:
                intent.missing_fields = ["appointment_id"]
                intent.validation_errors = {} if matches else {"appointment_id": "not_found"}
                intent.choices = [{"doctor": row.get("provider_name"), "site": row.get("location_name"),
                                   "when": row.get("when")} for row in (matches or appointments)]
                return [], None
            existing = matches[0]
            if not isinstance(existing.get("appointment_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", existing["appointment_id"]):
                raise ValueError("clinic returned an invalid appointment reference")
            if intent.action == "cancel":
                return [Offer(revision=intent.revision, action="cancel", payload={"appointment_id": existing["appointment_id"]},
                              display={"patient": patient.get("full_name") or intent.subject,
                                       "doctor": existing.get("provider_name", ""), "site": existing.get("location_name", ""),
                                       "when": existing.get("when", "")})], None
        if intent.action == "reschedule" and not (criteria.when or criteria.part_of_day or criteria.time_of_day):
            if criteria.doctor_name or criteria.location_id:
                previous_time = str(existing.get("slot") or existing.get("when") or "")
                criteria.when = previous_time[:10] or None
                criteria.time_of_day = previous_time[11:16] or None
            if not criteria.when:
                intent.missing_fields = ["when"]
                return [], None
        specialty = criteria.specialty_id or (existing or {}).get("specialty_id")
        provider = (existing or {}).get("provider_id") if not criteria.doctor_name else None
        if criteria.doctor_name:
            found = self.engine.resolve_provider(criteria.doctor_name)
            exact = [row for row in found["providers"] if normalize_provider_name(row["name"]) == normalize_provider_name(criteria.doctor_name)]
            providers = exact or found["providers"]
            if len(providers) != 1:
                intent.missing_fields = ["doctor_name"]
                intent.choices = [{"doctor": row["name"]} for row in providers]
                return [], "provider_not_found" if not providers else None
            provider = providers[0]["provider_id"]
            if specialty and specialty != providers[0]["specialty_id"]:
                intent.validation_errors = {"specialty_id": "provider_specialty_mismatch"}
                return [], None
            specialty = providers[0]["specialty_id"]
        if not specialty:
            intent.missing_fields = ["specialty_id"]
            return [], None
        if not provider:
            specialty = self.engine.age_appropriate_specialty(patient, specialty, state.reference_time) or specialty
        insurers = []
        for spoken in criteria.insurers:
            resolved = self.engine.resolve_insurer(spoken)
            if not resolved:
                intent.validation_errors = {"insurers": "unknown_plan"}
                return [], None
            if resolved not in insurers:
                insurers.append(resolved)
        eligible_patient = {**patient, "insurer": None} if insurers else patient
        blocked = self.engine.check_eligibility(eligible_patient, specialty, state.reference_time)
        if blocked:
            return [], blocked
        spec = resolve_request_when(criteria.when, state.reference_time)
        clock = criteria.time_of_day or requested_clock(criteria.when)
        if criteria.when and spec.matched == "unparsed" and not (spec.part_of_day or criteria.part_of_day or clock):
            intent.validation_errors = {"when": "unrecognized_date"}
            return [], None
        part = criteria.part_of_day or spec.part_of_day
        if spec.target_date and spec.target_date < state.reference_time.date():
            intent.validation_errors = {"when": "past_date"}
            return [], None
        if spec.target_date and not self.catalog.in_calendar(spec.target_date):
            return [], "no_availability"
        months = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
        search_when = f"{spec.target_date.day} {months[spec.target_date.month - 1]}" if spec.target_date else None
        search = await self.engine.find_slots(
            patient_id=patient["patient_id"], specialty_id=specialty, provider_id=provider,
            location_id=criteria.location_id or (existing or {}).get("location_id"),
            when=search_when, part_of_day=part,
            now=state.reference_time, language=criteria.clinician_language, limit=5000 if clock else 6,
            insurers=list(dict.fromkeys([patient.get("insurer"), *insurers])) if insurers and patient.get("insurer") else insurers or None,
        )
        if any(line.startswith("availability failed:") for line in search.trace):
            raise RuntimeError("clinic availability could not be verified")
        if (search.reason is not None and search.reason not in ALL_REASONS) or any(
                entry.get("restriction") not in ALL_REASONS for entry in search.blocked):
            raise RuntimeError("clinic returned an unknown restriction")
        offers = []
        candidates = search.slots
        if clock:
            exact = [slot for slot in candidates if slot.start.strftime("%H:%M") == clock]
            candidates = exact or candidates
        for slot in candidates:
            clinician = self.catalog.providers.get(slot.provider_id)
            location = self.catalog.locations.get(slot.location_id)
            if not clinician or not location or slot.appointment_type_id not in self.catalog.appointment_types:
                raise RuntimeError("availability contains unknown catalog references")
            if clinician.specialty_id != slot.specialty_id or slot.location_id not in clinician.location_ids:
                raise RuntimeError("availability contradicts the clinic catalog")
            if slot.duration_minutes <= 0 or slot.start.second or slot.start.microsecond:
                raise RuntimeError("availability contains an invalid time")
            requested_site = criteria.location_id or (existing or {}).get("location_id")
            if slot.specialty_id != specialty or (provider and slot.provider_id != provider) or (requested_site and slot.location_id != requested_site):
                continue
            if slot.start.date() <= state.reference_time.date() or not self.catalog.in_calendar(slot.start.date()):
                continue
            end = slot.start + timedelta(minutes=slot.duration_minutes)
            spans = location.hours.get(WEEKDAY_NAMES[slot.start.weekday()], [])
            if (not self.catalog.is_open(slot.start.date(), slot.location_id) or end.date() != slot.start.date()
                    or not any(start <= slot.start.time() and end.time() <= close for start, close in spans)):
                blocked = "location_hours"
                continue
            held = {patient.get("insurer"), *insurers} - {None, ""}
            payable = [plan for plan in slot.payable_with if plan in held]
            if not payable:
                blocked = "specialty_not_covered"
                continue
            if clinician.on_leave_on(slot.start.date()):
                blocked = "provider_on_leave"
                continue
            selected = None
            for candidate in list(dict.fromkeys([patient.get("insurer"), *insurers])):
                if candidate not in payable:
                    continue
                if not self.catalog.plan_covers_specialty(candidate, specialty):
                    blocked = "specialty_not_covered"
                elif not self.catalog.plan_covers_location(candidate, slot.location_id):
                    blocked = "location_not_covered"
                elif clinician and candidate in clinician.refused_insurers:
                    blocked = "provider_not_in_network"
                else:
                    selected = candidate
                    break
            if selected is None:
                continue
            plan, reason = self.engine.plan_booking({**patient, "insurer": selected}, slot, insurers)
            if plan is None:
                blocked = reason
                continue
            payload = plan.payload(state.call_id)
            payload.pop("call_id")
            if intent.action == "reschedule":
                payload = {k: payload[k] for k in ("provider_id", "location_id", "slot", "policy_id")}
                payload["appointment_id"] = existing["appointment_id"]
                if existing.get("slot") and parse_slot(existing["slot"]) == slot.start and existing.get("provider_id") == slot.provider_id and existing.get("location_id") == slot.location_id:
                    continue
            alternative = bool((spec.target_date and slot.start.date() != spec.target_date) or
                               (part and not matches_part_of_day(slot.start, part)) or
                               (clock and slot.start.strftime("%H:%M") != clock))
            location = self.catalog.locations.get(slot.location_id)
            offers.append(Offer(revision=intent.revision, action=intent.action, payload=payload, display={
                "patient": patient.get("full_name") or intent.subject, "doctor": slot.provider_name,
                "site": location.name if location else slot.location_id, "when": slot.start.strftime("%Y-%m-%d %H:%M"),
                "policy": selected, "alternative": alternative,
                "alternative_reason": "clinic_closed" if alternative and spec.target_date and not self.catalog.is_open(spec.target_date, requested_site) else "no_availability",
                "previous": {"doctor": existing.get("provider_name"), "site": existing.get("location_name"), "when": existing.get("when")} if existing else None,
            }))
            if len(offers) == 2:
                break
        return offers, None if offers else search.reason or blocked or "no_availability"

    def facts(self, topic: str, language: str = "en", location_id: str | None = None) -> dict:
        locations = [loc for loc in self.catalog.locations.values() if not location_id or loc.id == location_id]
        if topic == "sites":
            lines = [f"{loc.name}: {loc.address}" for loc in locations]
        elif topic == "doctors":
            lines = [p.name for p in self.catalog.providers.values()]
        elif topic == "specialties":
            lines = [s.name for s in self.catalog.specialties.values()]
        elif topic == "plans":
            lines = [str(p.get("name", key)) for key, p in self.catalog.plans.items()]
        else:
            names = {
                "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
                "ca": ["dilluns", "dimarts", "dimecres", "dijous", "divendres", "dissabte", "diumenge"],
            }
            days = dict(zip(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"), names[language]))
            lines = [f"{loc.name}, {days[day]}: " + ", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in spans)
                     for loc in locations for day, spans in loc.hours.items() if spans]
            if self.catalog.closure_days:
                label = {"en": "Closed", "es": "Cerrado", "ca": "Tancat"}[language]
                lines.append(label + ": " + ", ".join(str(day) for day in sorted(self.catalog.closure_days)))
        return {"topic": topic, "text": ". ".join(lines) + "."}

    async def close(self):
        await self.client.aclose()


class FixtureClinic:
    patients = {
        "lina demo": {"patient_id": "fixture-adult", "given_name": "Lina", "date_of_birth": "1990-01-01"},
        "roc demo": {"patient_id": "fixture-child", "given_name": "Roc", "date_of_birth": "2018-01-01"},
    }

    async def identify(self, identity: Identity) -> dict | None:
        row = self.patients.get((identity.name or "").casefold())
        return dict(row) if row and row["date_of_birth"] == identity.date_of_birth else None

    async def prepare(self, state: CallState, intent: Intent, op: Operation) -> tuple[list[Offer], str | None]:
        if op.when == "fully booked":
            return [], "no_availability"
        if intent.action == "register":
            fields = intent.registration_fields.model_dump(exclude_none=True)
            for patch in (op.registration, op.registration_fields):
                if patch:
                    fields.update(patch.model_dump(exclude_none=True))
            payload, intent.missing_fields, intent.validation_errors = registration_plan(
                SchedulingEngine(None, Catalog.load()), RegistrationFields(**fields), state.reference_time.date())
            return ([Offer(revision=intent.revision, action="register", payload=payload, display=payload)]
                    if payload else []), None
        if intent.action not in {"book", "cancel", "reschedule"}:
            return [], "out_of_scope"
        if intent.patient is None:
            return [], "patient_not_found" if intent.identity_status == "not_found" else None
        intent.missing_fields, intent.validation_errors = [], {}
        slot = (state.reference_time + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        while slot.weekday() == 6 or slot.date().isoformat() == "2026-10-12":
            slot += timedelta(days=1)
        display = {"patient": intent.subject, "doctor": "Doctor Demo", "site": "Centro",
                   "when": slot.strftime("%Y-%m-%d %H:%M")}
        if intent.action == "cancel":
            payload = {"appointment_id": "fixture-appointment-" + intent.patient["patient_id"]}
        elif intent.action == "reschedule":
            payload = {"appointment_id": "fixture-appointment-" + intent.patient["patient_id"],
                       "provider_id": "fixture-provider", "location_id": "centro", "slot": slot.isoformat(),
                       "policy_id": "fixture-policy"}
        else:
            payload = {"patient_id": intent.patient["patient_id"], "provider_id": "fixture-provider",
                       "location_id": "centro", "appointment_type_id": "fixture-type", "slot": slot.isoformat(),
                       "policy_id": "fixture-policy"}
        return [Offer(revision=intent.revision, action=intent.action, payload=payload, display=display)], None

    def facts(self, topic: str, language: str = "en", location_id: str | None = None) -> dict:
        return {**ClinicBoundary(None, Catalog.load()).facts(topic, language, location_id), "fixture": True}

    async def close(self):
        pass


class Dispatcher:
    def __init__(self, store: RunStore, mode: str, sender=None, allow_live: bool = False):
        self.store, self.mode, self.sender, self.allow_live = store, mode, sender, allow_live

    async def execute(self, state: CallState, intent: Intent, action: str, payload: dict) -> dict[str, Any]:
        if state.mode != self.mode:
            raise ValueError("run mode does not match the server-owned action sink")
        if action not in {"book", "cancel", "reschedule", "register", "no_action", "escalate"}:
            raise ValueError("unsupported action")
        if action in {"no_action", "escalate"} and payload.get("reason") not in ALL_REASONS:
            raise ValueError("invalid reason")
        if action == "no_action" and payload.get("reason") == "medical_emergency":
            raise ValueError("medical emergencies require escalation")
        required = {
            "book": {"patient_id", "provider_id", "location_id", "appointment_type_id", "slot", "policy_id"},
            "cancel": {"appointment_id"},
            "reschedule": {"appointment_id", "provider_id", "location_id", "slot", "policy_id"},
            "register": set(RegistrationFields.model_fields), "no_action": {"reason"}, "escalate": {"reason"},
        }[action]
        if set(payload) != required or any(not isinstance(value, str) or not value.strip() for value in payload.values()):
            raise ValueError("action payload is incomplete or contains untrusted fields")
        for key, value in payload.items():
            if key.endswith("_id") and key != "national_id" and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
                raise ValueError("malformed backend reference")
        if "slot" in payload:
            slot = parse_slot(payload["slot"])
            if slot.date() <= state.reference_time.date():
                raise ValueError("appointment must be after the call date")
        if action == "register":
            _, errors = identity_problems({key: payload[key] for key in ("date_of_birth", "national_id", "phone")}, state.reference_time.date())
            if errors:
                raise ValueError("invalid registration identifiers")
        if self.mode == "live" and (not self.allow_live or self.sender is None):
            raise PermissionError("live submissions are disabled")
        complete = {**payload, "call_id": state.call_id}
        key, previous = self.store.begin_action(state.run_id, intent.intent_id, action, complete)
        if previous is not None:
            return previous
        cancelled = False
        if self.mode != "live":
            receipt = {"action": action, "payload": complete, "accepted": True, "http_status": None,
                       "source": "simulation", "action_key": key}
        else:
            task = asyncio.create_task(self.sender(action, complete))
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                result = await task
                cancelled = True
            receipt = {"action": action, "payload": complete, "accepted": result.accepted or result.duplicate,
                       "http_status": result.status, "source": "platform_receipt", "action_key": key}
        self.store.finish_action(key, receipt)
        self.store.event(state.run_id, "action_receipt", receipt)
        if cancelled:
            raise asyncio.CancelledError
        return receipt
