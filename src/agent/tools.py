"""The tools the model is allowed to use, and what they hand back.

Every id, minute, appointment type and plan that reaches a submission comes
out of these functions, never out of the conversation.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any, Optional

from src.domain.catalog import age_months
from src.domain.engine import Slot
from src.domain.identity import normalize_provider_name, normalize_text, parse_national_id
from src.domain.outcomes import ALL_REASONS, is_valid_reason
from src.domain.timeref import format_slot, now_madrid
from src.domain.triage import triage

if TYPE_CHECKING:  # pragma: no cover
    from src.telephony.session import CallSession

REASON_LIST = ", ".join(ALL_REASONS)

# Refusals a second plan can undo. Nobody volunteers one, so the refusal waits
# until the caller has been asked.
INSURANCE_REASONS = frozenset({
    "specialty_not_covered", "location_not_covered", "provider_not_in_network",
    "insurer_referral_required", "allowance_exhausted",
})

# The agent asking about another plan, in the languages it speaks. Matched on
# normalised text (lowercase, no accents).
_OTHER_PLAN_QUESTION = re.compile(
    r"\b(other|another|second|different|additional)\b.{0,30}\b(insurance|insurer|plan|policy|cover)"
    r"|\b(otr[oa]s?|segund[oa]|algun[oa]?|mas)\b.{0,30}\b(seguros?|polizas?|mutuas?|aseguradoras?|planes?)"
    r"|\b(altr[ea]s?|segon[a]?|cap)\b.{0,30}\b(assegura\w*|mutu\w*|poliss\w*)"
)

SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lookup_patient",
            "description": (
                "Search the clinic directory. Ask the caller's name if they have not said it. If "
                "a caller-id lookup returned someone and the caller has not said a name, confirm "
                "with the name this tool returned, in their language (e.g. 'Am I speaking with "
                "Ella Smith?' / '¿Hablo con Ella Smith?'). Never say a name you have not been "
                "given, and never a placeholder. The caller id is a lookup field, not a reason "
                "to skip that. A name search needs the given name plus "
                "at least one surname; a surname on its own is not enough. Fields are compared "
                "exactly, so a misheard national id usually returns nobody — name plus date of "
                "birth is what separates two people who share a name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Full or partial name as said."},
                    "national_id": {"type": "string", "description": "DNI or NIE as dictated."},
                    "phone": {"type": "string", "description": "Any format; digits are compared."},
                    "date_of_birth": {"type": "string", "description": "YYYY-MM-DD."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_chart",
            "description": (
                "Open one patient's chart and make them the patient this call is about. "
                "Returns their note, visit history and any upcoming appointments. Call this "
                "before asking anything the chart already answers."
            ),
            "parameters": {
                "type": "object",
                "properties": {"patient_id": {"type": "string"}},
                "required": ["patient_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_doctor",
            "description": (
                "Resolve a doctor's name to the clinic's record of them, with their specialty, "
                "sites, languages and any leave. Two surname pairs sound alike on the phone and "
                "come back as two candidates — ask the caller which one they mean."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_symptom",
            "description": (
                "Route a described complaint to a specialty, and catch the red flags that must "
                "not be booked at all. Call this whenever the caller describes a symptom rather "
                "than naming a specialty."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "complaint": {"type": "string", "description": "What the caller described."},
                    "is_child": {"type": "boolean", "description": "True if the patient is under 14."},
                },
                "required": ["complaint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_appointments",
            "description": (
                "Real availability for one patient. Returns numbered options you can read out, "
                "or the rule that forbids it. Say the time phrase exactly as the caller said it "
                "in `when` — it is resolved against the clock in Madrid. If they later name a "
                "day or say 'first thing' / 'a primera hora', call again with that phrase; do "
                "not book a slot from a previous 'soonest' list. Never booked for the same day. "
                "Name the patient: slots carry their appointment type and their plan, so "
                "searching under the wrong person returns the wrong answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {
                        "type": "string",
                        "description": "Who this is for, as returned by open_chart. The caller is "
                                       "often not the patient, and a call can involve two people.",
                    },
                    "specialty_id": {
                        "type": "string",
                        "enum": ["general_practice", "paediatrics", "dermatology",
                                 "orthopaedics", "gynaecology", "physiotherapy"],
                    },
                    "provider_id": {"type": "string", "description": "From find_doctor, if they named one."},
                    "location_id": {"type": "string", "enum": ["centro", "norte", "sur"]},
                    "when": {
                        "type": "string",
                        "description": "The caller's own words for when: a day, a part of the day, "
                                       "a window, or that they want the soonest. Pass it as they said it.",
                    },
                    "part_of_day": {"type": "string", "enum": ["morning", "afternoon"]},
                    "complaint": {
                        "type": "string",
                        "description": "The symptom they described, if they did not name a specialty. "
                                       "Checked for red flags before anything is booked.",
                    },
                    "language": {
                        "type": "string",
                        "description": "Two-letter code, only when the caller needs a doctor who "
                                       "speaks it: 'ca', 'es', 'en'.",
                    },
                    "also_consider_insurer": {
                        "type": "string",
                        "description": "A second plan the caller mentioned on the call. Only a "
                                       "named plan can be quoted against.",
                    },
                },
                "required": ["patient_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nearest_site",
            "description": (
                "Which site is closest to where the caller says they are, and whether it can "
                "serve what they need. Pass the address, street, plaza or town as they said it. "
                "Always call this when they describe a place instead of naming Centro, Norte or "
                "Sur. Book at `nearest_serving`, not at the geographically closest site if that "
                "one cannot do what they asked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "where": {"type": "string"},
                    "specialty_id": {"type": "string"},
                },
                "required": ["where"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clinic_facts",
            "description": (
                "The clinic's own details for answering questions: sites and their opening "
                "hours, doctors, specialties, insurance plans. Use this rather than remembering."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string",
                              "enum": ["sites", "hours", "doctors", "specialties", "plans"]},
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_slot",
            "description": (
                "Book one of the numbered options from find_appointments. Confirm the day, time "
                "and doctor with the caller first, then call this. Name the patient it is for: a "
                "call can involve more than one person, and the appointment goes to whoever's "
                "chart is open, not to whoever is speaking."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "option": {"type": "integer", "description": "The option number you read out."},
                    "patient_id": {
                        "type": "string",
                        "description": "Who the appointment is for, as returned by open_chart.",
                    },
                    "caller_confirmed": {
                        "type": "boolean",
                        "description": (
                            "True only when the caller explicitly accepted this exact option "
                            "in their latest turn. A symptom or a follow-up question is not consent."
                        ),
                    },
                },
                "required": ["option", "patient_id", "caller_confirmed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": "Move an existing appointment to one of the numbered options.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string", "description": "From the chart."},
                    "option": {"type": "integer"},
                },
                "required": ["appointment_id", "option"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": (
                "Cancel one existing appointment from the chart. Each cancellation is its own "
                "call to this tool. Pass the chart id when you have it; if they named a doctor "
                "or have only one upcoming appointment, that is enough to identify the row. "
                "Do not cancel appointments they did not ask to cancel."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "string",
                        "description": "From the chart. Optional when there is only one upcoming, "
                                       "or when doctor_name uniquely identifies it.",
                    },
                    "doctor_name": {
                        "type": "string",
                        "description": "The doctor they named, if they did not give an id.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_new_patient",
            "description": (
                "Put a caller the directory does not know on file. Call as soon as every field "
                "has been heard, including a missing DNI/NIE letter — it is re-derived from the "
                "digits. If STT glued the id and the phone into one number, pass both as heard. "
                "The first call sends nothing: it hands back the details spelled out for you to "
                "read back once. When the caller says they are right, call it again with the same "
                "details and confirmed=true. One wrong character fails the record. "
                "Nothing is booked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmed": {
                        "type": "boolean",
                        "description": "True only after the caller confirmed the read-back.",
                    },
                    "given_name": {"type": "string"},
                    "first_surname": {"type": "string"},
                    "second_surname": {"type": "string"},
                    "national_id": {"type": "string"},
                    "date_of_birth": {"type": "string", "description": "YYYY-MM-DD."},
                    "phone": {"type": "string"},
                    "email": {"type": "string", "description": "As dictated; 'punto' and 'arroba' are understood."},
                    "insurer": {"type": "string"},
                },
                "required": ["given_name", "first_surname", "second_surname", "national_id",
                             "date_of_birth", "phone", "email", "insurer"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_without_booking",
            "description": (
                "End the call with no appointment, carrying the reason. Use the reason a tool "
                f"named where there was one. One of: {REASON_LIST}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "enum": list(ALL_REASONS)},
                    "explanation": {"type": "string", "description": "One line for the record."},
                    "alternatives_declined": {
                        "type": "boolean",
                        "description": (
                            "For provider-specific refusals, true only after the caller explicitly "
                            "declined another doctor in the same specialty."
                        ),
                    },
                    "caller_has_no_other_plan": {
                        "type": "boolean",
                        "description": "True only once you asked whether they hold another "
                                       "insurance plan and they said they do not.",
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_call",
            "description": (
                "Hand the call to a human. Use `medical_emergency` for a red flag, after telling "
                "the caller to seek urgent care."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "enum": list(ALL_REASONS)},
                    "explanation": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
]


class ToolBox:
    """Per-call tool state. Nothing here is shared between calls."""

    def __init__(self, session: "CallSession") -> None:
        self.session = session
        self.engine = session.engine
        self.catalog = session.catalog
        self.patient: Optional[dict[str, Any]] = None
        self.patient_context: dict[str, Any] = {}
        self.options: dict[int, Slot] = {}
        # A call can move to a second patient, and the options on the table were
        # priced and typed for the first one.
        self.options_for: Optional[str] = None
        self.named_insurers: list[str] = []
        self.last_reason: Optional[str] = None
        # What was last read back to a new patient; only that is ever registered.
        self.pending_registration: Optional[dict[str, Any]] = None
        self.lookup_attempted = False
        # Provider ids a tool has actually handed over on this call. The briefing
        # lists every doctor by name, which is enough for the model to resolve a
        # spoken surname itself and skip the ambiguity find_doctor exists to
        # surface.
        self.resolved_providers: set[str] = set()
        self.registration_attempted = False
        self.last_register_fields: Optional[dict[str, Any]] = None

    def schemas(self) -> list[dict[str, Any]]:
        return SCHEMAS

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"error": f"no such tool: {name}"}
        return await handler(arguments)

    # ---- lookups ------------------------------------------------------

    async def _tool_lookup_patient(self, args: dict[str, Any]) -> dict[str, Any]:
        self.lookup_attempted = True
        result = await self.engine.identify(
            name=args.get("name"),
            national_id=args.get("national_id"),
            phone=args.get("phone"),
            date_of_birth=args.get("date_of_birth"),
        )
        await self.session.remember_matches(result["matches"])
        if result.get("needs_more"):
            return {
                "found": 0,
                "needs_more": True,
                "guidance": "Not enough to search on. The directory needs a given name plus at "
                            "least one surname, or an exact national id, phone or date of birth. "
                            "Ask the caller for one of those.",
                "trace": result["trace"],
            }
        if result["count"] == 0:
            self.last_reason = "patient_not_found"
            return {
                "found": 0,
                "guidance": "Nobody on file matches. Confirm the spelling and the date of birth; "
                            "if they have genuinely never been here, offer to register them.",
                "trace": result["trace"],
            }
        if result["ambiguous"]:
            return {
                "found": result["count"],
                "matches": [
                    {k: v for k, v in match.items() if k not in ("note", "phone")}
                    for match in result["matches"]
                ],
                "ask_for": result["distinguishers"],
                "guidance": "Several people match. Ask for one of `ask_for` before going further.",
            }
        # Two fields matching is the confirmation; asking for a third costs a
        # turn out of a three-minute call.
        searched = [key for key in ("name", "national_id", "phone", "date_of_birth") if args.get(key)]
        guidance = (
            f"Matched on {' and '.join(searched)}, which confirms who this is. Do not ask for "
            "another field; open the chart."
            if len(searched) >= 2 else
            "Only one field matched. Confirm one more with the caller (their name or date of "
            "birth), then open the chart."
        )
        return {"found": 1, "match": result["matches"][0], "guidance": guidance}

    async def _tool_open_chart(self, args: dict[str, Any]) -> dict[str, Any]:
        patient_id = str(args.get("patient_id", "")).strip()
        context = await self.engine.patient_context(patient_id)
        found = self.patient if self.patient and self.patient.get("patient_id") == patient_id else None
        if found is None:
            # Recover the demographics from whichever lookup produced this id.
            found = self._remembered(patient_id) or {"patient_id": patient_id}

        if self.patient is not None and self.patient.get("patient_id") != patient_id:
            # A second patient on the same call inherits nothing from the first:
            # not their options, not a plan they mentioned, not why they were
            # refused, and not which doctors a tool already resolved. All four
            # would otherwise bill or book the wrong person.
            self.options = {}
            self.options_for = None
            self.named_insurers = []
            self.last_reason = None
            self.resolved_providers.clear()

        self.patient = found
        self.patient_context = context
        # The doctors on their own chart are a tool's answer too, so "my usual
        # doctor" does not have to go back through find_doctor.
        for appointment in (context.get("upcoming") or []) + (context.get("past") or []):
            if appointment.get("provider_id"):
                self.resolved_providers.add(appointment["provider_id"])
        await self.session.on_patient_identified(found, context)
        return {
            "patient": {k: v for k, v in found.items() if k != "national_id"},
            "visit_count": context["visit_count"],
            "last_visit": context["last_visit"],
            "upcoming": context["upcoming"],
            "recent_past": context["past"],
            "guidance": "Use this before asking. Never read the national id or phone aloud "
                        "unless the caller gave it to you first.",
        }

    def _remembered(self, patient_id: str) -> Optional[dict[str, Any]]:
        for match in self.session.seen_patients:
            if match.get("patient_id") == patient_id:
                return match
        return None

    async def _tool_find_doctor(self, args: dict[str, Any]) -> dict[str, Any]:
        result = self.engine.resolve_provider(str(args.get("name", "")))
        for provider in result.get("providers", []):
            self.resolved_providers.add(provider["provider_id"])
        if result["count"] == 0:
            self.last_reason = "provider_not_found"
            return {"found": 0, "guidance": "No doctor by that name. Offer the specialty instead."}
        if result["ambiguous"]:
            return {**result, "guidance": "Two names sound alike. Ask which specialty they need."}
        return result

    async def _tool_check_symptom(self, args: dict[str, Any]) -> dict[str, Any]:
        result = triage(str(args.get("complaint", "")), args.get("is_child"))
        if result.escalate:
            self.last_reason = "medical_emergency"
            return {
                "emergency": True,
                "red_flag": result.red_flag,
                "guidance": "This is a red flag. Tell them to seek urgent care now and call "
                            "escalate_call with medical_emergency. Book nothing.",
            }
        if result.specialty_id is None:
            return {"emergency": False, "specialty_id": None,
                    "guidance": "Not conclusive. Ask one more question, or ask the patient's age."}
        specialty = self.catalog.specialties.get(result.specialty_id)
        return {
            "emergency": False,
            "specialty_id": result.specialty_id,
            "specialty_name": specialty.name if specialty else result.specialty_id,
            "referral_required": bool(specialty and specialty.referral_required),
        }

    async def _tool_clinic_facts(self, args: dict[str, Any]) -> dict[str, Any]:
        topic = str(args.get("topic", "sites"))
        summary = self.catalog.summary()
        if topic in {"sites", "hours"}:
            return {"locations": [
                {
                    "location_id": loc.id, "name": loc.name, "address": loc.address,
                    "hours": {day: [f"{s.isoformat(timespec='minutes')}-{e.isoformat(timespec='minutes')}"
                                    for s, e in spans] for day, spans in loc.hours.items()},
                }
                for loc in self.catalog.locations.values()
            ], "closed": {"sundays": True,
                          "closure_days": summary["calendar"]["closure_days"],
                          "saturday_open": ["centro"]}}
        if topic == "doctors":
            return {"providers": summary["providers"]}
        if topic == "specialties":
            return {"specialties": summary["specialties"]}
        return {"plans": [
            {"id": plan["id"], "name": plan["name"],
             "uncovered_specialties": plan["uncovered_specialty_names"],
             "uncovered_locations": plan["uncovered_location_names"],
             "refused_by": plan["refused_by"]}
            for plan in self.catalog.plans.values()
        ], "pricing_available": False,
            "guidance": "The clinic has coverage rules but no policy prices or copays. "
                        "Direct price questions to the insurer without inventing a figure."}

    async def _tool_nearest_site(self, args: dict[str, Any]) -> dict[str, Any]:
        result = self.engine.nearest_site(str(args.get("where", "")), args.get("specialty_id"))
        serving = result.get("nearest_serving") or {}
        if not result.get("resolved"):
            result["guidance"] = (
                "Could not place that address. Ask for the town or district, then call again."
            )
            return result
        location_id = serving.get("location_id")
        if not location_id:
            result["guidance"] = (
                "No site can serve that specialty. Tell the caller so, and call "
                "end_without_booking if they still want it."
            )
            return result
        result["guidance"] = (
            "Tell the caller which site that is, in one sentence. Then pass "
            f"`location_id={location_id}` to find_appointments. Do not pick a site from memory "
            "or from the addresses in the briefing — the closest site that cannot serve them "
            "is the wrong answer."
        )
        return result

    # ---- availability -------------------------------------------------

    async def _tool_find_appointments(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.patient is None:
            return {"error": "no chart is open",
                    "guidance": "Identify the patient and open their chart first."}
        # Caught here rather than at booking: by then the slots already carry the
        # wrong patient's appointment type and plan.
        wrong = self._wrong_chart(args.get("patient_id"))
        if wrong is not None:
            return wrong

        provider_id = args.get("provider_id")
        if provider_id and provider_id not in self.resolved_providers:
            return {
                "error": f"{provider_id} did not come from a tool on this call",
                "guidance": "Call find_doctor with the name the caller actually said. Two pairs "
                            "of surnames here are indistinguishable over a phone, so it returns "
                            "both and which one they mean is a question for the caller.",
            }

        # A plan id is lowercase and underscored ("nueva_mutua") but the caller
        # says "Nueva Mutua". An unrecognised name used to be dropped in silence,
        # which the model reads as a second policy that did not help.
        unrecognised: Optional[str] = None
        spoken_insurer = str(args.get("also_consider_insurer") or "").strip()
        if spoken_insurer:
            extra = self.engine.resolve_insurer(spoken_insurer)
            if extra is not None:
                if extra not in self.named_insurers:
                    self.named_insurers.append(extra)
            else:
                unrecognised = spoken_insurer

        insurers: Optional[list[str]] = None
        if self.named_insurers:
            own = self.patient.get("insurer")
            insurers = ([own] if own else []) + self.named_insurers

        # A Catalan speaker is booked with a doctor who speaks Catalan, whether or
        # not the model thinks to ask for one. Only Catalan: every doctor speaks
        # Spanish, and not every doctor speaks English.
        language = args.get("language") or ("ca" if self.session.language == "ca" else None)

        # The age boundary is the clinic's rule, not the caller's problem: asking
        # for "the doctor" for an eight-year-old is paediatrics, not a refusal.
        specialty_id = args.get("specialty_id")
        complaint = str(args.get("complaint") or "").strip()
        if complaint:
            is_child = None
            born = (self.patient or {}).get("date_of_birth")
            if born:
                try:
                    is_child = age_months(date.fromisoformat(str(born)[:10]), now_madrid().date()) < 168
                except ValueError:
                    is_child = None
            triaged = triage(complaint, is_child)
            if triaged.escalate:
                self.last_reason = "medical_emergency"
                return {
                    "options": [],
                    "emergency": True,
                    "red_flag": triaged.red_flag,
                    "guidance": "This is a red flag. Tell them to seek urgent care now and call "
                                "escalate_call with medical_emergency. Book nothing.",
                }
            if not specialty_id and triaged.specialty_id:
                specialty_id = triaged.specialty_id
        rerouted = self.engine.age_appropriate_specialty(self.patient, specialty_id)
        if rerouted:
            specialty_id = rerouted

        location_id = args.get("location_id")
        if location_id and specialty_id:
            serving = self.catalog.locations_serving(specialty_id)
            if serving and location_id not in serving:
                return {
                    "options": [],
                    "location_cannot_serve": True,
                    "requested_location": location_id,
                    "locations_that_serve": sorted(serving),
                    "guidance": (
                        f"{location_id} has no {specialty_id}. The sites that do are "
                        f"{sorted(serving)}. Tell the caller the closest of those cannot do "
                        "what they need, then search again with one of `locations_that_serve` "
                        "(the nearest from nearest_site, if you have it)."
                    ),
                }

        search = await self.engine.find_slots(
            patient_id=self.patient.get("patient_id"),
            specialty_id=specialty_id,
            provider_id=provider_id,
            location_id=location_id,
            when=args.get("when"),
            part_of_day=args.get("part_of_day"),
            language=language,
            insurers=insurers,
        )

        await self.session.record("decision", {
            "stage": "availability",
            "asked": {k: v for k, v in args.items() if v},
            "language_filter": language,
            "language_source": ("model" if args.get("language") else "stt") if language else None,
            "rerouted_by_age": rerouted,
            "window": [d.isoformat() if d else None for d in search.window],
            "found": len(search.slots),
            "reason": search.reason,
            "trace": search.trace,
            "blocked": search.blocked,
        })

        if not search.found:
            self.last_reason = search.reason
            return {
                "options": [],
                "reason": search.reason,
                "blocked": search.blocked,
                "trace": search.trace,
                **self._unknown_plan(unrecognised),
                "guidance": (
                    "Nothing available. If `reason` names a clinic rule, explain that rule to the "
                    "caller and call end_without_booking with it. If the caller may hold another "
                    "insurance plan, asking is the only way to find out."
                ),
            }

        self.options = {index + 1: slot for index, slot in enumerate(search.slots)}
        self.options_for = self.patient.get("patient_id")
        return {
            # Echoed so a read-back names the right person out loud.
            "searched_for": {
                "patient_id": self.patient.get("patient_id"),
                "name": self.patient.get("full_name"),
            },
            "specialty_used": specialty_id,
            **self._unknown_plan(unrecognised),
            "rerouted_by_age": rerouted,
            "appointment_type_id": search.appointment_type_id,
            "asked_for": search.when.describe() if search.when else None,
            "exact_day_available": search.exact_day_met,
            "requested_part_of_day_exists": search.part_of_day_possible,
            "notes": search.notes,
            "options": [
                {
                    "option": index,
                    "when": slot.start.strftime("%A %d %B, %H:%M"),
                    "slot": format_slot(slot.start),
                    "doctor": slot.provider_name,
                    "provider_id": slot.provider_id,
                    "site": self.catalog.locations[slot.location_id].name
                    if slot.location_id in self.catalog.locations else slot.location_id,
                    "location_id": slot.location_id,
                    "minutes": slot.duration_minutes,
                }
                for index, slot in self.options.items()
            ],
            "guidance": (
                "Read out one or two of these, not all of them. "
                + ("" if search.exact_day_met else
                   "What they asked for exactly was not free, so say that first and let them "
                   "choose between what is here. ")
                + ("" if search.part_of_day_possible else
                   "Nothing falls in the part of day they asked for. Tell them that plainly. "
                   "Do not book a different time of day unless they explicitly accept it. "
                   "If they cannot move, call end_without_booking with no_availability. ")
                + ("These are the first appointments that morning — offer the earliest. "
                   if search.when and search.when.first_thing else "")
                + ("Mention what `notes` says before offering these. " if search.notes else "")
                + "Then call book_slot with the option number they choose."
            ),
        }

    # ---- writes -------------------------------------------------------

    async def _tool_book_slot(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("caller_confirmed") is not True:
            return {
                "booked": False,
                "error": "the caller has not explicitly accepted this exact option",
                "guidance": "Read back the day, time and doctor, then wait for a clear yes.",
            }
        slot = self.options.get(int(args.get("option", 0) or 0))
        if slot is None:
            return {"error": "that option is not on the table",
                    "guidance": "Call find_appointments again and offer what it returns."}
        if self.patient is None:
            return {"error": "no chart is open"}
        wrong = self._wrong_chart(args.get("patient_id"))
        if wrong is not None:
            return wrong
        stale = self._stale_options()
        if stale is not None:
            return stale

        plan, reason = self.engine.plan_booking(self.patient, slot, self.named_insurers)
        if plan is None:
            self.last_reason = reason
            return {"booked": False, "reason": reason,
                    "guidance": "This cannot be booked. Explain the rule and call "
                                "end_without_booking with this reason."}

        result = await self.session.submit("book", plan.payload(self.session.call_id))
        await self.session.record("decision", {
            "stage": "booked",
            "why": f"caller accepted option {args.get('option')}",
            "payload": plan.payload(self.session.call_id),
            "warnings": plan.warnings,
        })
        return {
            "booked": result.accepted or result.duplicate,
            "status": result.status,
            "confirmed": {
                "when": slot.start.strftime("%A %d %B, %H:%M"),
                "doctor": plan.provider_name,
                "site": plan.location_id,
            },
            "reference_number": None,
            "guidance": "Read the appointment back once and close warmly. The platform did not "
                        "provide a reference number, so never invent one.",
        }

    async def _tool_reschedule_appointment(self, args: dict[str, Any]) -> dict[str, Any]:
        slot = self.options.get(int(args.get("option", 0) or 0))
        appointment_id = str(args.get("appointment_id", "")).strip()
        if slot is None:
            return {"error": "that option is not on the table"}
        if not self._known_appointment(appointment_id):
            return {"error": "that appointment is not on the chart",
                    "guidance": "Only an upcoming appointment from open_chart can be moved."}
        stale = self._stale_options()
        if stale is not None:
            return stale

        policy = self._policy_for(slot)
        if policy is None:
            self.last_reason = "specialty_not_covered"
            return {"moved": False, "reason": "specialty_not_covered"}

        payload = {
            "call_id": self.session.call_id,
            "appointment_id": appointment_id,
            "provider_id": slot.provider_id,
            "location_id": slot.location_id,
            "slot": format_slot(slot.start),
            "policy_id": policy,
        }
        result = await self.session.submit("reschedule", payload)
        await self.session.record("decision", {"stage": "rescheduled", "payload": payload})
        return {
            "moved": result.accepted or result.duplicate,
            "status": result.status,
            "confirmed": {"when": slot.start.strftime("%A %d %B, %H:%M"),
                          "doctor": slot.provider_name},
            "guidance": "Read the new time back to the caller.",
        }

    async def _tool_cancel_appointment(self, args: dict[str, Any]) -> dict[str, Any]:
        appointment_id = self._resolve_upcoming(
            str(args.get("appointment_id") or "").strip(),
            str(args.get("doctor_name") or "").strip(),
        )
        if not appointment_id:
            upcoming = self.patient_context.get("upcoming") or []
            return {
                "error": "which appointment is not clear",
                "upcoming": upcoming,
                "guidance": "Name the doctor or the time from the chart, then call again. "
                            "If they have two, cancel only the one they asked for.",
            }
        result = await self.session.submit(
            "cancel", {"call_id": self.session.call_id, "appointment_id": appointment_id}
        )
        await self.session.record("decision", {"stage": "cancelled", "appointment_id": appointment_id})
        leftover = [
            row for row in (self.patient_context.get("upcoming") or [])
            if row.get("appointment_id") != appointment_id
        ]
        self.patient_context["upcoming"] = leftover
        return {
            "cancelled": result.accepted or result.duplicate,
            "status": result.status,
            "appointment_id": appointment_id,
            "still_on_the_chart": leftover,
            "guidance": "Confirm this cancellation. If they asked to cancel another one as well, "
                        "call this tool again for that row. Do not cancel leftover appointments "
                        "unless they asked.",
        }

    async def _tool_register_new_patient(self, args: dict[str, Any]) -> dict[str, Any]:
        self.registration_attempted = True
        self.last_register_fields = dict(args)
        # A failed directory lookup is expected on a register call; it is not the outcome.
        if self.last_reason == "patient_not_found":
            self.last_reason = None
        fields, problems = self.engine.plan_registration(args)
        if fields is None:
            return {"registered": False, "problems": problems,
                    "guidance": "Ask only for the fields in `problems`, then call again. "
                                "A missing check letter is filled in from the digits — do not "
                                "hold the call to hear it. If the id and phone arrived as one "
                                "number, pass them as heard."}

        if not args.get("confirmed") or fields != self.pending_registration:
            # A corrected detail changes the fields, which lands back here: what
            # is registered is always exactly what the caller last heard.
            self.pending_registration = fields
            letter_inferred = bool(parse_national_id(str(args.get("national_id", "")))["letter_missing"])
            await self.session.record("decision", {
                "stage": "register_read_back", "fields": fields,
                "letter_inferred": letter_inferred,
            })
            return {
                "registered": False,
                "needs_confirmation": True,
                "read_back": _read_back(fields, self.session.language),
                "letter_inferred": letter_inferred,
                "guidance": "Nothing is on file yet. Read these back in one turn: spell the given "
                            "name and both surnames, give the id digit by digit with its letter"
                            + (" (the caller did not say the letter; it was worked out from the "
                               "digits, so ask them to confirm it)" if letter_inferred else "")
                            + ", and spell the email. Ask whether it is all correct. If yes, call "
                              "register_new_patient again with the same details and confirmed=true; "
                              "if they correct anything, call it again with the corrected details "
                              "and no confirmed.",
            }

        payload = {"call_id": self.session.call_id, **fields}
        result = await self.session.submit("register", payload)
        await self.session.record("decision", {"stage": "registered", "payload": payload})
        return {
            "registered": result.accepted or result.duplicate,
            "status": result.status,
            "on_file": {"name": f"{fields['given_name']} {fields['first_surname']} {fields['second_surname']}",
                        "national_id": fields["national_id"]},
            "guidance": "Confirm they are on file and say goodbye in one short utterance. "
                        "Nothing is booked on this call; do not offer a slot or add generic filler.",
        }

    async def _tool_end_without_booking(self, args: dict[str, Any]) -> dict[str, Any]:
        reason = self._clean_reason(args.get("reason"))
        # A named doctor who cannot see them is a redirect before it is a refusal.
        redirectable = {"provider_not_in_network", "provider_on_leave", "provider_not_found"}
        if reason in redirectable and args.get("alternatives_declined") is not True:
            return {
                "recorded": False,
                "reason": reason,
                "guidance": "Do not close the record yet. Offer another doctor in the same "
                            "specialty; only retry after the caller explicitly declines.",
            }
        # The flag is the model's word; what the agent actually said on the call
        # is what shows the question was asked.
        if (reason in INSURANCE_REASONS and not self.named_insurers
                and not (args.get("caller_has_no_other_plan") and self._asked_about_other_plan())):
            await self.session.record("decision", {
                "stage": "refusal_held", "reason": reason,
                "why": "an insurance refusal waits until the caller is asked about a second plan",
            })
            return {
                "recorded": False,
                "error": "ask_second_plan",
                "guidance": "Before refusing, ask whether they hold another insurance plan. If "
                            "they name one, call find_appointments again with "
                            "also_consider_insurer. Only if they say they have no other plan, "
                            "call end_without_booking again with caller_has_no_other_plan=true.",
            }
        result = await self.session.submit(
            "no_action", {"call_id": self.session.call_id, "reason": reason}
        )
        await self.session.record("decision", {
            "stage": "no_action", "reason": reason,
            "why": args.get("explanation") or "",
        })
        return {"recorded": result.accepted or result.duplicate, "reason": reason,
                "guidance": "Tell the caller plainly why, in one sentence, and close politely."}

    async def _tool_escalate_call(self, args: dict[str, Any]) -> dict[str, Any]:
        reason = self._clean_reason(args.get("reason"))
        result = await self.session.submit(
            "escalate", {"call_id": self.session.call_id, "reason": reason}
        )
        await self.session.record("decision", {
            "stage": "escalated", "reason": reason, "why": args.get("explanation") or "",
        })
        return {"recorded": result.accepted or result.duplicate, "reason": reason,
                "guidance": "Stay on the line with them for one more sentence, then close."}

    # ---- helpers ------------------------------------------------------

    def _wrong_chart(self, wanted: Any) -> Optional[dict[str, Any]]:
        """Refuse to book for anyone other than the patient whose chart is open.

        On a two-intent call the model tends to move the second person's
        appointment and then book the caller's own without reopening their
        chart, which quietly writes the appointment against the wrong patient.
        Making it name the patient turns that into a correction it can act on.
        """
        wanted_id = str(wanted or "").strip()
        current = (self.patient or {}).get("patient_id")
        if not wanted_id or wanted_id == current:
            return None
        return {
            "error": f"the open chart is {current}, not {wanted_id}",
            "guidance": f"Open {wanted_id}'s chart, run find_appointments again for them, and "
                        "offer what it returns. Slots found under another patient carry that "
                        "patient's appointment type and plan.",
        }

    def _stale_options(self) -> Optional[dict[str, Any]]:
        """Refuse to write when the options belong to a different patient.

        The slots on the table carry the appointment type and the plans that
        were resolved for whoever the search ran against. Writing them under a
        second patient produces a booking that looks right and bills wrong,
        which is worse than asking the model to search again.
        """
        current = (self.patient or {}).get("patient_id")
        if self.options_for is None or self.options_for == current:
            return None
        return {
            "error": "those options were found for a different patient",
            "found_for": self.options_for,
            "chart_open": current,
            "guidance": "Call find_appointments again now this chart is open, then offer what "
                        "it returns.",
        }

    def _unknown_plan(self, spoken: Optional[str]) -> dict[str, Any]:
        """Say so when a plan the caller named is not one the clinic has."""
        if not spoken:
            return {}
        return {
            "plan_not_recognised": spoken,
            "known_plans": sorted(self.catalog.plans),
            "plan_guidance": f"{spoken!r} is not one of the clinic's plans, so nothing was quoted "
                             "against it. Ask the caller which insurer they hold and try again "
                             "with the name from `known_plans`.",
        }

    def _asked_about_other_plan(self) -> bool:
        said = " ".join(
            str(message.get("content") or "")
            for message in self.session.agent.messages
            if message.get("role") == "assistant"
        )
        return bool(_OTHER_PLAN_QUESTION.search(normalize_text(said)))

    def _known_appointment(self, appointment_id: str) -> bool:
        upcoming = self.patient_context.get("upcoming") or []
        return any(a.get("appointment_id") == appointment_id for a in upcoming)

    def _resolve_upcoming(self, appointment_id: str, doctor_name: str = "") -> Optional[str]:
        """Pick the chart row they meant, including a vague 'my appointment'."""
        upcoming = self.patient_context.get("upcoming") or []
        if appointment_id and self._known_appointment(appointment_id):
            return appointment_id
        query = normalize_provider_name(doctor_name)
        if query:
            hits = []
            for row in upcoming:
                name = normalize_provider_name(str(row.get("provider_name") or ""))
                if query == name or query in name or any(
                    token in name.split() for token in query.split() if len(token) > 3
                ):
                    hits.append(row)
            unique = list({row.get("appointment_id"): row for row in hits}.values())
            if len(unique) == 1:
                return unique[0].get("appointment_id")
        if len(upcoming) == 1:
            return upcoming[0].get("appointment_id")
        return None

    def _policy_for(self, slot: Slot) -> Optional[str]:
        payable = [plan for plan in slot.payable_with if plan]
        own = (self.patient or {}).get("insurer")
        if own and own in payable:
            return own
        for insurer in self.named_insurers:
            if insurer in payable:
                return insurer
        unique = list(dict.fromkeys(payable))
        return unique[0] if len(unique) == 1 else None

    def _clean_reason(self, reason: Any) -> str:
        candidate = str(reason or "").strip()
        if is_valid_reason(candidate):
            return candidate
        if self.last_reason and is_valid_reason(self.last_reason):
            return self.last_reason
        return "out_of_scope"

    def completable_registration(self) -> Optional[dict[str, Any]]:
        """A registration payload that was heard but never accepted, if one exists."""
        if not self.last_register_fields:
            return None
        fields, problems = self.engine.plan_registration(self.last_register_fields)
        return fields if fields and not problems else None

    def fallback_reason(self) -> str:
        """The reason to report if the call ends before the model closes it."""
        if self.last_reason and is_valid_reason(self.last_reason):
            if not (self.registration_attempted and self.last_reason == "patient_not_found"):
                return self.last_reason
        if self.patient is None and not self.registration_attempted:
            # Nobody was ever looked up: not a caller we failed to find, but a
            # call that was never about booking (a request for another
            # patient's data, a sales call). Problem 14 expects out_of_scope.
            return "patient_not_found" if self.lookup_attempted else "out_of_scope"
        return "no_availability"


def _spell(text: str) -> str:
    """"Nuria" -> "N-U-R-I-A": a misheard letter is heard when read one at a time."""
    return "-".join(ch.upper() for ch in text if not ch.isspace())


# How the symbols in an address are said, per call language.
_EMAIL_WORDS = {
    "en": {"@": "at", ".": "dot", "_": "underscore", "-": "dash"},
    "es": {"@": "arroba", ".": "punto", "_": "guion bajo", "-": "guion"},
    "ca": {"@": "arrova", ".": "punt", "_": "guió baix", "-": "guió"},
}


def _spell_email(email: str, language: str = "en") -> str:
    words = _EMAIL_WORDS.get((language or "")[:2], _EMAIL_WORDS["en"])
    local, _, domain = email.partition("@")
    spelled = " ".join(words.get(ch, ch.upper()) for ch in local)
    return f"{spelled} {words['@']} {domain.replace('.', ' ' + words['.'] + ' ')}"


def _read_back(fields: dict[str, Any], language: str = "en") -> dict[str, str]:
    return {
        "given_name": _spell(fields["given_name"]),
        "first_surname": _spell(fields["first_surname"]),
        "second_surname": _spell(fields["second_surname"]),
        "national_id": " ".join(fields["national_id"]),
        "date_of_birth": fields["date_of_birth"],
        "phone": " ".join(fields["phone"]),
        "email": _spell_email(fields["email"], language),
        "insurer": fields["insurer"],
    }
