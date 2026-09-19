from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from src.domain.catalog import Catalog
from src.domain.engine import SchedulingEngine
from src.domain.identity import normalize_provider_name
from src.domain.outcomes import ALL_REASONS
from src.platform_api.client import PlatformClient
from v2.models import CallState, Identity, Intent, Offer, Operation
from v2.store import RunStore


class ClinicBoundary:
    def __init__(self, client: PlatformClient, catalog: Catalog):
        self.client = client
        self.engine = SchedulingEngine(client, catalog)
        self.catalog = catalog

    async def identify(self, identity: Identity) -> dict | None:
        fields = identity.model_dump(exclude_none=True)
        if len([v for v in fields.values() if v]) < 2:
            return None
        result = await self.engine.identify(**fields)
        if result["count"] != 1:
            return None
        patient = dict(result["matches"][0])
        patient["upcoming"] = (await self.engine.patient_context(patient["patient_id"]))["upcoming"]
        return patient

    async def prepare(self, state: CallState, intent: Intent, op: Operation) -> tuple[list[Offer], str | None]:
        revision = intent.revision
        if intent.action == "register":
            if op.registration is None:
                return [], None
            fields, problems = self.engine.plan_registration(op.registration.model_dump())
            if problems:
                return [], None
            return [Offer(revision=revision, action="register", payload=fields, display=fields)], None
        if intent.patient is None:
            return [], "patient_not_found"
        patient = intent.patient
        appointments = patient.get("upcoming", [])
        existing = None
        if intent.action in {"cancel", "reschedule"}:
            if op.appointment_id:
                matches = [row for row in appointments if row["appointment_id"] == op.appointment_id]
            elif op.doctor_name:
                query = normalize_provider_name(op.doctor_name)
                matches = [row for row in appointments if normalize_provider_name(row["provider_name"]) == query]
            else:
                matches = appointments if len(appointments) == 1 else []
            if len(matches) != 1:
                return [], None
            existing = matches[0]
            if intent.action == "cancel":
                return [Offer(revision=revision, action="cancel",
                              payload={"appointment_id": existing["appointment_id"]}, display={
                                  "patient": patient.get("given_name", intent.subject),
                                  "doctor": existing.get("provider_name", ""),
                                  "site": existing.get("location_name", ""), "when": existing.get("when", ""),
                              })], None
        if intent.action not in {"book", "reschedule"}:
            return [], "out_of_scope"
        specialty = op.specialty_id or (existing or {}).get("specialty_id")
        provider = None
        if op.doctor_name and intent.action == "book":
            found = self.engine.resolve_provider(op.doctor_name)
            if found["count"] != 1:
                return [], None
            provider = found["providers"][0]["provider_id"]
            specialty = found["providers"][0]["specialty_id"]
        if not specialty:
            return [], None
        specialty = self.engine.age_appropriate_specialty(patient, specialty, state.reference_time) or specialty
        blocked = self.engine.check_eligibility(patient, specialty, state.reference_time)
        if blocked:
            return [], blocked
        search = await self.engine.find_slots(
            patient_id=patient["patient_id"], specialty_id=specialty, provider_id=provider,
            location_id=op.location_id, when=op.when, now=state.reference_time,
            language="ca" if state.language == "ca" else None,
        )
        offers = []
        for slot in search.slots[:2]:
            plan, reason = self.engine.plan_booking(patient, slot)
            if plan is None:
                blocked = reason
                continue
            payload = plan.payload(state.call_id)
            payload.pop("call_id")
            if intent.action == "reschedule":
                payload = {k: payload[k] for k in ("provider_id", "location_id", "slot", "policy_id")}
                payload["appointment_id"] = existing["appointment_id"]
            offers.append(Offer(revision=revision, action=intent.action, payload=payload, display={
                "patient": patient.get("given_name", intent.subject), "doctor": slot.provider_name,
                "site": slot.location_id, "when": slot.start.strftime("%Y-%m-%d %H:%M"),
                "alternative": not search.exact_day_met or not search.part_of_day_possible,
            }))
        return offers, search.reason or blocked

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
            if op.registration is None:
                return [], None
            fields, problems = SchedulingEngine(None, Catalog.load()).plan_registration(op.registration.model_dump())
            return ([Offer(revision=intent.revision, action="register", payload=fields, display=fields)]
                    if not problems else []), None
        if intent.patient is None:
            return [], "patient_not_found"
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
