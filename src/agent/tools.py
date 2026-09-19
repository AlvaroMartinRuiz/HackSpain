"""The tools the model is allowed to use, and what they hand back.

Every id, minute, appointment type and plan that reaches a submission comes
out of these functions, never out of the conversation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from src.domain.engine import Slot
from src.domain.outcomes import ALL_REASONS, is_valid_reason
from src.domain.timeref import format_slot, now_madrid
from src.domain.triage import triage

if TYPE_CHECKING:  # pragma: no cover
    from src.telephony.session import CallSession

REASON_LIST = ", ".join(ALL_REASONS)

SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lookup_patient",
            "description": (
                "Search the clinic directory. The caller id is already a lookup, so try phone "
                "first. A name search needs the given name plus at least one surname; a surname "
                "on its own is not enough. Fields are compared exactly, so a misheard national "
                "id usually returns nobody — name plus date of birth is what separates two "
                "people who share a name."
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
                "Real availability for the patient whose chart is open. Returns numbered options "
                "you can read out, or the rule that forbids it. Say the time phrase exactly as "
                "the caller said it in `when` — it is resolved against the clock in Madrid. "
                "Never booked for the same day."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "specialty_id": {
                        "type": "string",
                        "enum": ["general_practice", "paediatrics", "dermatology",
                                 "orthopaedics", "gynaecology", "physiotherapy"],
                    },
                    "provider_id": {"type": "string", "description": "From find_doctor, if they named one."},
                    "location_id": {"type": "string", "enum": ["centro", "norte", "sur"]},
                    "when": {
                        "type": "string",
                        "description": "The caller's own words: 'el jueves que viene', 'mañana', "
                                       "'first thing Monday', 'lo antes posible'.",
                    },
                    "part_of_day": {"type": "string", "enum": ["morning", "afternoon"]},
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
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nearest_site",
            "description": (
                "Which site is closest to where the caller says they are, and whether it can "
                "serve what they need. Pass the address or town as they said it."
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
                "and doctor with the caller first, then call this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "option": {"type": "integer", "description": "The option number you read out."},
                },
                "required": ["option"],
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
                "Cancel one existing appointment from the chart. Two cancellations on one call "
                "are two separate calls to this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {"appointment_id": {"type": "string"}},
                "required": ["appointment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_new_patient",
            "description": (
                "Put a caller the directory does not know on file. Every field is checked: the "
                "national id's own letter is re-derived from its digits, so read it back before "
                "calling this. Nothing is booked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
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
        self.named_insurers: list[str] = []
        self.last_reason: Optional[str] = None

    def schemas(self) -> list[dict[str, Any]]:
        return SCHEMAS

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"error": f"no such tool: {name}"}
        return await handler(arguments)

    # ---- lookups ------------------------------------------------------

    async def _tool_lookup_patient(self, args: dict[str, Any]) -> dict[str, Any]:
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
        return {"found": 1, "match": result["matches"][0],
                "guidance": "Confirm one more field with the caller, then open the chart."}

    async def _tool_open_chart(self, args: dict[str, Any]) -> dict[str, Any]:
        patient_id = str(args.get("patient_id", "")).strip()
        context = await self.engine.patient_context(patient_id)
        found = self.patient if self.patient and self.patient.get("patient_id") == patient_id else None
        if found is None:
            # Recover the demographics from whichever lookup produced this id.
            found = self._remembered(patient_id) or {"patient_id": patient_id}

        self.patient = found
        self.patient_context = context
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
        ]}

    async def _tool_nearest_site(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.engine.nearest_site(str(args.get("where", "")), args.get("specialty_id"))

    # ---- availability -------------------------------------------------

    async def _tool_find_appointments(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.patient is None:
            return {"error": "no chart is open",
                    "guidance": "Identify the patient and open their chart first."}

        extra = args.get("also_consider_insurer")
        if extra and extra in self.catalog.plans and extra not in self.named_insurers:
            self.named_insurers.append(extra)

        insurers: Optional[list[str]] = None
        if self.named_insurers:
            own = self.patient.get("insurer")
            insurers = ([own] if own else []) + self.named_insurers

        search = await self.engine.find_slots(
            patient_id=self.patient.get("patient_id"),
            specialty_id=args.get("specialty_id"),
            provider_id=args.get("provider_id"),
            location_id=args.get("location_id"),
            when=args.get("when"),
            part_of_day=args.get("part_of_day"),
            language=args.get("language"),
            insurers=insurers,
        )

        await self.session.record("decision", {
            "stage": "availability",
            "asked": {k: v for k, v in args.items() if v},
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
                "guidance": (
                    "Nothing available. If `reason` names a clinic rule, explain that rule to the "
                    "caller and call end_without_booking with it. If the caller may hold another "
                    "insurance plan, asking is the only way to find out."
                ),
            }

        self.options = {index + 1: slot for index, slot in enumerate(search.slots)}
        return {
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
                   "This specialty has no appointments at that time of day at all — tell them "
                   "that plainly instead of implying there might be. ")
                + ("Mention what `notes` says before offering these. " if search.notes else "")
                + "Then call book_slot with the option number they choose."
            ),
        }

    # ---- writes -------------------------------------------------------

    async def _tool_book_slot(self, args: dict[str, Any]) -> dict[str, Any]:
        slot = self.options.get(int(args.get("option", 0) or 0))
        if slot is None:
            return {"error": "that option is not on the table",
                    "guidance": "Call find_appointments again and offer what it returns."}
        if self.patient is None:
            return {"error": "no chart is open"}

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
            "guidance": "Read the appointment back to the caller and close warmly.",
        }

    async def _tool_reschedule_appointment(self, args: dict[str, Any]) -> dict[str, Any]:
        slot = self.options.get(int(args.get("option", 0) or 0))
        appointment_id = str(args.get("appointment_id", "")).strip()
        if slot is None:
            return {"error": "that option is not on the table"}
        if not self._known_appointment(appointment_id):
            return {"error": "that appointment is not on the chart",
                    "guidance": "Only an upcoming appointment from open_chart can be moved."}

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
        appointment_id = str(args.get("appointment_id", "")).strip()
        if not self._known_appointment(appointment_id):
            return {"error": "that appointment is not on the chart",
                    "guidance": "Only an upcoming appointment from open_chart can be cancelled. "
                                "A past visit cannot."}
        result = await self.session.submit(
            "cancel", {"call_id": self.session.call_id, "appointment_id": appointment_id}
        )
        await self.session.record("decision", {"stage": "cancelled", "appointment_id": appointment_id})
        return {"cancelled": result.accepted or result.duplicate, "status": result.status,
                "guidance": "Confirm the cancellation, and ask whether anything else is needed — "
                            "a caller cancelling two appointments says so here."}

    async def _tool_register_new_patient(self, args: dict[str, Any]) -> dict[str, Any]:
        fields, problems = self.engine.plan_registration(args)
        if fields is None:
            return {"registered": False, "problems": problems,
                    "guidance": "Ask the caller to confirm exactly these details, then call again. "
                                "For the id, read the digits back and confirm the final letter."}

        payload = {"call_id": self.session.call_id, **fields}
        result = await self.session.submit("register", payload)
        await self.session.record("decision", {"stage": "registered", "payload": payload})
        return {
            "registered": result.accepted or result.duplicate,
            "status": result.status,
            "on_file": {"name": f"{fields['given_name']} {fields['first_surname']} {fields['second_surname']}",
                        "national_id": fields["national_id"]},
            "guidance": "They are on file now. Nothing is booked on this call — do not offer a slot.",
        }

    async def _tool_end_without_booking(self, args: dict[str, Any]) -> dict[str, Any]:
        reason = self._clean_reason(args.get("reason"))
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

    def _known_appointment(self, appointment_id: str) -> bool:
        upcoming = self.patient_context.get("upcoming") or []
        return any(a.get("appointment_id") == appointment_id for a in upcoming)

    def _policy_for(self, slot: Slot) -> Optional[str]:
        payable = set(slot.payable_with)
        own = (self.patient or {}).get("insurer")
        if own and own in payable:
            return own
        for insurer in self.named_insurers:
            if insurer in payable:
                return insurer
        return sorted(payable)[0] if payable else None

    def _clean_reason(self, reason: Any) -> str:
        candidate = str(reason or "").strip()
        if is_valid_reason(candidate):
            return candidate
        if self.last_reason and is_valid_reason(self.last_reason):
            return self.last_reason
        return "out_of_scope"

    def fallback_reason(self) -> str:
        """The reason to report if the call ends before the model closes it."""
        if self.last_reason and is_valid_reason(self.last_reason):
            return self.last_reason
        if self.patient is None:
            return "patient_not_found"
        return "no_availability"
