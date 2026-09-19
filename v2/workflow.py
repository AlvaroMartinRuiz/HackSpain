from __future__ import annotations

import asyncio
import re
import time
from typing import TypedDict

from langgraph.errors import NodeCancelledError
from langgraph.graph import END, START, StateGraph
from langsmith.run_helpers import tracing_context

from v2.domain.identity import normalize_text
from v2.domain.triage import triage
from v2.language import decide_language, should_apply_language
from v2.clinic import Dispatcher
from v2.models import MAX_CALL_TURNS, CallState, Identity, Intent, Operation, RegistrationFields, Reply, SchedulingCriteria, TurnDecision
from v2.domain_rules import FIELDS, collection_prompt, confirmation_selection, spoken_when, without_repeats, explicit_decline, identity_problems, reason_text, requested_clock, resolve_request_when, unsupported_time_request
from v2.store import RunStore

TEXT = {
    "en": {
        "identity": "Please give the patient's full name and date of birth.",
        "appointment": "Which appointment do you need, and when would you like it?",
        "registration_identity": "Please tell me your national identity number, including its letter.",
        "registration_contact": "Please tell me your phone number, email address and insurance plan.",
        "clarify": "I need to clarify that before taking any action. Please confirm the patient and the requested appointment.",
        "confirm": "Please confirm the exact option before I proceed.",
        "remaining": "There is still another request to resolve. How can I help with it?",
        "done": "All your requests have been resolved. Goodbye.",
        "received": "The action has been recorded.",
        "simulation": "Simulation only: the action has been recorded locally.",
        "empty": "There is no available option for that request.",
        "emergency": "Please seek urgent medical care now. I cannot book a routine appointment for these symptoms.",
        # Most callers speak English; "buenos días" tells a Spanish caller they can answer in Spanish.
        "hello": "Clínica Arenal, good morning, buenos días. How can I help?",
        "facts": "I can help with clinic locations, doctors, insurance and appointments. Which detail do you need?",
    },
    "es": {
        "identity": "Dígame el nombre completo y la fecha de nacimiento del paciente.",
        "appointment": "¿Qué cita necesita y para cuándo la quiere?",
        "registration_identity": "Dígame su DNI o NIE, incluida la letra, por favor.",
        "registration_contact": "Dígame su teléfono, correo electrónico y aseguradora, por favor.",
        "clarify": "Necesito aclararlo antes de hacer nada. Confirme el paciente y la cita que necesita.",
        "confirm": "Confirme la opción exacta antes de continuar, por favor.",
        "remaining": "Todavía queda otra solicitud por resolver. ¿Qué necesita para ella?",
        "done": "Todas sus solicitudes están resueltas. Hasta luego.",
        "received": "La acción ha quedado registrada.",
        "simulation": "Solo es una simulación: la acción se ha registrado localmente.",
        "empty": "No hay una opción disponible para esa solicitud.",
        "emergency": "Busque atención médica urgente ahora. No puedo reservar una cita ordinaria para estos síntomas.",
        "hello": "Clínica Arenal, buenos días. ¿En qué puedo ayudarle?",
        "facts": "Puedo ayudarle con sedes, médicos, seguros y citas. ¿Qué detalle necesita?",
    },
    "ca": {
        "identity": "Digui'm el nom complet i la data de naixement del pacient.",
        "appointment": "Quina cita necessita i per a quan la vol?",
        "registration_identity": "Digui'm el seu DNI o NIE, inclosa la lletra, si us plau.",
        "registration_contact": "Digui'm el seu telèfon, correu electrònic i asseguradora, si us plau.",
        "clarify": "Ho he d'aclarir abans de fer res. Confirmi el pacient i la cita que necessita.",
        "confirm": "Confirmi l'opció exacta abans de continuar, si us plau.",
        "remaining": "Encara queda una altra petició per resoldre. Què necessita per a aquesta petició?",
        "done": "Totes les seves peticions estan resoltes. Adeu.",
        "received": "L'acció ha quedat registrada.",
        "simulation": "Només és una simulació: l'acció s'ha registrat localment.",
        "empty": "No hi ha cap opció disponible per a aquesta petició.",
        "emergency": "Busqui atenció mèdica urgent ara. No puc reservar una cita ordinària per a aquests símptomes.",
        "hello": "Clínica Arenal, bon dia. Com el puc ajudar?",
        "facts": "Puc ajudar-lo amb centres, metges, assegurances i cites. Quin detall necessita?",
    },
}


CA_URGENT = re.compile(r"\b(mal|dolor)\b.*\bpit\b|no (puc|pot) respirar")


def urgent_language(text: str, current: str) -> str:
    clean = normalize_text(text)
    if CA_URGENT.search(clean):
        return "ca"
    if re.search(r"\b(chest|breathe|bleeding|slurred|droop|head)\b", clean):
        return "en"
    if re.search(r"\b(pecho|respirar|sangra|hemorragia|golpe|cara)\b", clean):
        return "es"
    return current


def explicit_acceptance(text: str) -> bool:
    return confirmation_selection(text)[0]


class Flow(TypedDict):
    call: CallState
    text: str
    decision: TurnDecision | None
    epoch: int
    reply: Reply | None


class CallController:
    def __init__(self, state: CallState, clinic, store: RunStore, dispatcher: Dispatcher, interpreter=None,
                 manifest: dict | None = None):
        self.state, self.clinic, self.store = state, clinic, store
        self.dispatcher, self.interpreter = dispatcher, interpreter
        self.epoch = 0
        self.lock = asyncio.Lock()
        self.interpret_task: asyncio.Task | None = None
        self.pending_reply: Reply | None = None
        self.unanswered: list[str] = []
        self.answered_turns = 0
        self.store.save(state, manifest)
        graph = StateGraph(Flow)
        graph.add_node("interpret", self._interpret)
        graph.add_node("apply", self._apply)
        graph.add_edge(START, "interpret")
        graph.add_edge("interpret", "apply")
        graph.add_edge("apply", END)
        self.graph = graph.compile()

    def interrupt(self, source: str = "caller"):
        self.epoch += 1
        self.state.completion_requested = False
        self.pending_reply = None
        if source != "caller_input":
            for intent in self.state.intents.values():
                for offer in intent.offers:
                    offer.presented_turn = None
        if self.interpret_task is not None:
            self.interpret_task.cancel()
        self.store.event(self.state.run_id, "interruption" if source == "caller" else "response_cancelled",
                         {"epoch": self.epoch, "source": source})

    async def turn(self, text: str, decision: TurnDecision | None = None, *, expected_epoch: int | None = None) -> Reply | None:
        if not text.strip() or len(text) > 2000:
            raise ValueError("turn must contain between 1 and 2000 characters")
        async with self.lock:
            if expected_epoch is not None and expected_epoch != self.epoch:
                self._carry(text)
                return None
            # A caller who pauses mid-sentence arrives as several fragments; the ones that never
            # got an answer are part of what they said, not noise to drop.
            if self.unanswered:
                text = " ".join([*self.unanswered, text])[-2000:]
                self.unanswered = []
            self.pending_reply = None
            self.state.history = [*self.state.history[-7:], {"role": "caller", "text": text}]
            self.state.turn += 1
            if self.state.turn > MAX_CALL_TURNS:
                raise ValueError("call turn limit exceeded")
            self.state.completion_requested = False
            epoch = self.epoch
            started = time.monotonic()
            self.store.event(self.state.run_id, "caller_turn", {"text": text, "turn": self.state.turn})
            try:
                with tracing_context(enabled=False):
                    result = await self.graph.ainvoke({"call": self.state, "text": text, "decision": decision,
                                                      "epoch": epoch, "reply": None},
                                                     {"recursion_limit": 6})
            except (asyncio.CancelledError, NodeCancelledError):
                # LangGraph reports a cancelled interpretation as NodeCancelledError; either way the
                # caller moved on before this fragment was answered or applied.
                if epoch != self.epoch and not asyncio.current_task().cancelling():
                    if self.state.history and self.state.history[-1] == {"role": "caller", "text": text}:
                        self.state.history = self.state.history[:-1]
                    self._carry(text)
                    return None
                raise
            except Exception as exc:
                self.store.event(self.state.run_id, "error", {"where": "turn", "error_type": type(exc).__name__})
                self.state.completion_requested = False
                result = {"reply": Reply(text=reason_text(None, self.state.language),
                                         language=self.state.language, epoch=epoch)}
            finally:
                self.interpret_task = None
                self.state.revision += 1
                self.store.save(self.state)
            if epoch != self.epoch:
                return None
            reply = result["reply"]
            self.pending_reply = reply
            self.answered_turns += 1
            self.store.event(self.state.run_id, "response_planned", {
                **reply.model_dump(), "elapsed_ms": round((time.monotonic() - started) * 1000),
            })
            return reply

    def _carry(self, text: str):
        self.unanswered = [" ".join([*self.unanswered, text])[-2000:]]

    async def _interpret(self, flow: Flow) -> dict:
        if self.state.emergency or triage(flow["text"]).escalate or CA_URGENT.search(normalize_text(flow["text"])):
            return {"decision": TurnDecision(language=urgent_language(flow["text"], self.state.language))}
        if flow["decision"] is not None:
            return {}
        if self.interpreter is None:
            raise RuntimeError("a configured interpreter is required")
        self.interpret_task = asyncio.create_task(self.interpreter.decide(flow["text"], self.state))
        decision = await self.interpret_task
        self.interpret_task = None
        return {"decision": decision}

    async def _apply(self, flow: Flow) -> dict:
        decision, text = flow["decision"], flow["text"]
        # Established once the caller has been answered; fragments that were never answered do not count.
        established = self.answered_turns > 0
        detected = decide_language(text, decision.language, self.state.language, established=established)
        if decision.language == "ca" and re.search(r"\b(vull|voldria|puc|meva|meu|bon dia|si us plau)\b", normalize_text(text)):
            self.state.language = "ca"
        elif should_apply_language(self.state.language, established, detected):
            self.state.language = detected.code
        language = self.state.language
        lines, offered = [], {}
        red_flag = triage(text).escalate or bool(CA_URGENT.search(normalize_text(text)))
        proposed_escalation = any(op.op == "escalate" and op.reason == "medical_emergency"
                                 and op.evidence.strip() and normalize_text(op.evidence) in normalize_text(text)
                                 for op in decision.operations)
        if flow["epoch"] != self.epoch:
            return {"reply": None}
        if red_flag or self.state.emergency or proposed_escalation:
            self.state.emergency = True
            for pending in self.state.intents.values():
                pending.offers = []
            intent = self.state.intents.setdefault("urgent", Intent(intent_id="urgent", action="escalate", subject="caller"))
            if intent.action != "escalate":
                raise ValueError("reserved emergency reference is already in use")
            if intent.status != "completed" and not intent.submission_uncertain:
                try:
                    await self._submit(intent, "escalate", {"reason": "medical_emergency"})
                except Exception:
                    self.store.event(self.state.run_id, "emergency_receipt_unverified", {})
            self.state.completion_requested = intent.status == "completed"
            return {"reply": Reply(text=reason_text("medical_emergency", language), language=language, epoch=flow["epoch"],
                                    completion=self.state.completion_requested)}
        try:
            consent = self._preflight(decision.operations, text)
        except ValueError:
            self.store.event(self.state.run_id, "guard_rejected", {"operation": "confirmation", "error_type": "ValueError"})
            prepared_ids = [op.intent_id for op in decision.operations if op.op == "prepare"]
            if len(set(prepared_ids)) != len(prepared_ids):
                for intent_id in set(prepared_ids):
                    intent = self.state.intents.get(intent_id)
                    if intent and intent.status != "completed":
                        intent.offers = []
                        intent.revision += 1
                        intent.status = "collecting"
                return {"reply": Reply(text=TEXT[language]["clarify"], language=language, epoch=flow["epoch"])}
            terminal_ids = {op.intent_id for op in decision.operations if op.op in {"confirm", "refuse", "escalate"}}
            corrections = [op for op in decision.operations if op.op in {"identify", "prepare"} and op.intent_id in terminal_ids]
            for correction in corrections:
                if flow["epoch"] != self.epoch:
                    break
                self._evidence(correction, text)
                line = await self._operate(correction, text, offered)
                if line:
                    lines.append(line)
            return {"reply": Reply(text=" ".join(lines) or (self._next_prompt() if corrections else TEXT[language]["confirm"]),
                                    language=language, epoch=flow["epoch"], offers=offered)}
        operations = self._expand_creates([op for op in decision.operations if op.op != "finish"], text)
        finish = any(op.op == "finish" for op in decision.operations)
        failed = False
        for op in operations:
            if flow["epoch"] != self.epoch:
                break
            try:
                line = await self._operate(op, consent if op.op == "confirm" else text, offered)
                if op.op == "identify" and op.intent_id in self.state.intents:
                    identified = self.state.intents[op.intent_id]
                    already_preparing = any(other.op == "prepare" and other.intent_id == op.intent_id for other in operations)
                    ready = identified.action in {"cancel", "reschedule"} or identified.criteria.specialty_id or identified.criteria.doctor_name
                    if ((identified.patient and ready) or identified.action == "register") and not already_preparing:
                        line = await self._operate(Operation(op="prepare", intent_id=op.intent_id, evidence=op.evidence), text, offered)
                if line:
                    lines.append(line)
            except (ValueError, PermissionError, RuntimeError) as exc:
                self.store.event(self.state.run_id, "guard_rejected", {"operation": op.op,
                                                                       "error_type": type(exc).__name__})
                lines.append(TEXT[language]["clarify"])
                self.state.completion_requested = False
                failed = True
                break
        if finish and not failed:
            self.state.completion_requested = self.state.all_resolved
            lines.append(TEXT[language]["done"] if self.state.all_resolved else self._next_prompt())
        return {"reply": Reply(text=without_repeats(" ".join(lines)) or self._next_prompt(), language=language,
                                epoch=flow["epoch"], offers=offered,
                                completion=self.state.completion_requested and self.state.all_resolved)}

    def _preflight(self, operations: list[Operation], text: str) -> str:
        for terminal in (op for op in operations if op.op in {"refuse", "escalate"}):
            if any(op.intent_id == terminal.intent_id and op.op in {"identify", "prepare", "confirm"} for op in operations):
                raise ValueError("a refusal cannot resolve a request being corrected in the same turn")
        prepared_ids = [op.intent_id for op in operations if op.op == "prepare"]
        if len(set(prepared_ids)) != len(prepared_ids):
            raise ValueError("a request can only be prepared once in a turn")
        confirms = [op for op in operations if op.op == "confirm"]
        if not confirms:
            return text
        confirmed_ids = {op.intent_id for op in confirms}
        if len(confirmed_ids) != len(confirms):
            raise ValueError("an intent cannot be confirmed twice in one turn")
        if any(op.intent_id in confirmed_ids and op.op in {"create", "identify", "prepare", "refuse", "escalate"} for op in operations):
            raise ValueError("a correction cannot also confirm the old offer")
        consent = text
        additions = [op for op in operations if op.op == "create"]
        if additions:
            for op in additions:
                self._evidence(op, text)
                if not op.action or not op.subject or op.intent_id in self.state.intents:
                    raise ValueError("invalid additional request")
            parts = re.split(r"\.\s+(?:also|tambien|tambe)\b", normalize_text(text), maxsplit=1)
            if len(parts) == 2:
                consent = parts[0]
        accepted, selection, collective = confirmation_selection(consent)
        if not accepted or (len(confirms) > 1 and not collective):
            raise ValueError("unambiguous acceptance is required")
        presented = [(intent.intent_id, max((o.presented_turn or -1 for o in intent.offers), default=-1))
                     for intent in self.state.intents.values() if intent.status == "awaiting_confirmation"]
        latest = max((turn for _, turn in presented), default=-1)
        latest_ids = {key for key, turn in presented if turn == latest}
        if not confirmed_ids <= latest_ids:
            raise ValueError("acceptance must refer to the most recently presented request")
        if len(latest_ids) > 1 and not (collective and confirmed_ids == latest_ids):
            raise ValueError("multiple presented requests need collective acceptance")
        if collective and confirmed_ids != {key for key, turn in presented if turn >= 0}:
            raise ValueError("collective acceptance must cover all presented requests")
        for op in confirms:
            self._evidence(op, text)
            intent = self.state.intents.get(op.intent_id)
            if intent is None:
                raise ValueError("unknown intent")
            self._confirmed_offer(intent, op, consent)
        return consent

    def _confirmed_offer(self, intent: Intent, op: Operation, text: str):
        accepted, selected, _ = confirmation_selection(text)
        if intent.status != "awaiting_confirmation" or intent.submission_uncertain:
            raise ValueError("intent is not ready for confirmation")
        if not accepted or op.offer_revision != intent.revision or not op.option:
            raise ValueError("explicit acceptance of the current offer is required")
        if op.option > len(intent.offers) or (selected is not None and selected != op.option):
            raise ValueError("spoken selection must match the chosen option")
        if len(intent.offers) > 1 and selected is None:
            raise ValueError("multiple options require a spoken selection")
        offer = intent.offers[op.option - 1]
        if offer.revision != intent.revision or offer.action != intent.action:
            raise ValueError("offer no longer matches this request")
        if offer.presented_turn is None or offer.presented_turn >= self.state.turn:
            raise ValueError("the offer has not been presented to the caller")
        return offer

    async def _submit(self, intent: Intent, action: str, payload: dict) -> dict:
        if intent.submission_uncertain:
            raise RuntimeError("previous action requires reconciliation")
        try:
            receipt = await self.dispatcher.execute(self.state, intent, action, payload)
        except BaseException:
            intent.submission_uncertain = True
            intent.status = "blocked"
            intent.offers = []
            raise
        if type(receipt.get("accepted")) is not bool:
            intent.submission_uncertain = True
            intent.status = "blocked"
            intent.offers = []
            raise RuntimeError("action outcome is unknown")
        intent.receipts.append(receipt)
        intent.status = "completed" if receipt["accepted"] else "blocked"
        intent.offers = []
        if receipt["accepted"] and action in {"cancel", "reschedule"}:
            appointment = payload.get("appointment_id")
            for other in self.state.intents.values():
                if other.patient and intent.patient and other.patient.get("patient_id") == intent.patient.get("patient_id"):
                    other.patient["upcoming"] = [row for row in other.patient.get("upcoming", []) if row.get("appointment_id") != appointment]
                    if other is not intent and any(o.payload.get("appointment_id") == appointment for o in other.offers):
                        other.offers = []
                        other.revision += 1
                        other.status = "collecting"
        return receipt

    def _next_prompt(self) -> str:
        language = self.state.language
        unresolved = [intent for intent in self.state.intents.values() if intent.status != "completed"]
        if not unresolved:
            return TEXT[language]["remaining"] if self.state.intents else TEXT[language]["identity"]
        intent = unresolved[-1]
        if intent.submission_uncertain:
            return reason_text(None, language)
        if intent.blocking_reason:
            return reason_text(intent.blocking_reason, language)
        if intent.offers:
            return TEXT[language]["confirm"]
        return collection_prompt(intent, language)

    async def _operate(self, op: Operation, text: str, offered: dict) -> str | None:
        language = self.state.language
        if op.op == "finish":
            self.state.completion_requested = self.state.all_resolved
            return TEXT[language]["done" if self.state.all_resolved else "remaining"]
        if op.op == "ask":
            intent = self.state.intents.get(op.intent_id) if op.intent_id else None
            if intent and (intent.missing_fields or intent.validation_errors):
                return collection_prompt(intent, language)
            return self._next_prompt() if self.state.intents else TEXT[language][op.question or "clarify"]
        if op.op == "facts":
            facts = self.clinic.facts(op.topic or "sites", language, op.location_id)
            self.store.event(self.state.run_id, "clinic_facts", facts)
            return facts["text"]
        if not op.intent_id:
            raise ValueError("intent reference required")
        if op.op == "create":
            if not op.action or not op.subject or op.intent_id in self.state.intents or op.intent_id == "urgent":
                raise ValueError("a new intent needs an action and a unique reference")
            self._evidence(op, text)
            intent = Intent(intent_id=op.intent_id, action=op.action, subject=op.subject)
            intent.missing_fields = list(RegistrationFields.model_fields) if op.action == "register" else ["name", "date_of_birth"]
            self.state.intents[op.intent_id] = intent
            return None
        intent = self.state.intents.get(op.intent_id)
        if intent is None or intent.status == "completed":
            raise ValueError("unknown or already resolved intent")
        if intent.submission_uncertain:
            return reason_text(None, language)
        if op.op == "identify":
            self._evidence(op, text)
            if op.identity is None:
                raise ValueError("identity fields required")
            if intent.offers:
                intent.revision += 1
            intent.offers, intent.choices = [], []
            offered.pop(intent.intent_id, None)
            patch = op.identity.model_dump(exclude_none=True)
            previous = intent.identity_inputs
            if patch.get("name") and previous.get("name") and normalize_text(patch["name"]) != normalize_text(previous["name"]):
                previous = {}
                intent.criteria = intent.criteria.model_copy(update={"appointment_id": None, "appointment_when": None,
                                                                      "appointment_doctor_name": None, "appointment_location_id": None,
                                                                      "appointment_time": None})
                intent.registration_fields = RegistrationFields()
            intent.identity_inputs = {**previous, **patch}
            intent.patient = None
            intent.blocking_reason = None
            intent.status = "collecting"
            if intent.action == "register":
                fields = intent.registration_fields.model_dump(exclude_none=True)
                fields.update({key: value for key, value in patch.items() if key in {"date_of_birth", "national_id", "phone"}})
                intent.registration_fields = RegistrationFields(**fields)
                intent.identity_status = "partial"
                return None
            intent.missing_fields, intent.validation_errors = identity_problems(intent.identity_inputs, self.state.reference_time.date())
            if intent.missing_fields or intent.validation_errors:
                intent.identity_status = "invalid" if intent.validation_errors else "partial"
                return None
            identity = Identity(**intent.identity_inputs)
            if hasattr(self.clinic, "identify_result"):
                result = await self.clinic.identify_result(identity, self.state.reference_time.date())
                intent.patient = result["patient"]
                intent.identity_status = result["status"]
                intent.missing_fields = result.get("missing_fields", [])
                intent.validation_errors = result.get("validation_errors", {})
            else:
                intent.patient = await self.clinic.identify(identity)
                intent.identity_status = "verified" if intent.patient else "not_found"
            if intent.patient is not None:
                intent.patient = dict(intent.patient)
                intent.subject = intent.patient.get("full_name") or intent.identity_inputs.get("name") or intent.subject
                intent.missing_fields = ["appointment_id"] if intent.action in {"cancel", "reschedule"} else ["specialty_id"]
            elif intent.identity_status == "not_found":
                intent.blocking_reason = "patient_not_found"
            return None
        if op.op == "prepare":
            self._evidence(op, text)
            intent.revision += 1
            intent.offers = []
            intent.blocking_reason = None
            intent.status = "collecting"
            offered.pop(intent.intent_id, None)
            values = intent.criteria.model_dump()
            defaults = SchedulingCriteria().model_dump()
            for key in op.clear_fields:
                values[key] = defaults[key]
            previous_when = values["when"]
            previous_spec = resolve_request_when(previous_when, self.state.reference_time)
            if "part_of_day" in op.clear_fields or "time_of_day" in op.clear_fields:
                values["when"] = previous_spec.target_date.isoformat() if previous_spec.target_date else None
            if op.criteria:
                known_plans = {normalize_text(value).replace("_", " ") for value in intent.criteria.insurers}
                for insurer in op.criteria.insurers:
                    named = normalize_text(insurer).replace("_", " ")
                    if named not in known_plans and named not in normalize_text(text).replace("_", " "):
                        intent.validation_errors = {"insurers": "caller_must_name_plan"}
                        return collection_prompt(intent, language)
                values.update(op.criteria.updates())
            for key in ("specialty_id", "when", "doctor_name", "location_id", "appointment_id"):
                if getattr(op, key) is not None:
                    values[key] = getattr(op, key)
            changed_when = op.when or (op.criteria.when if op.criteria else None)
            if changed_when:
                if unsupported_time_request(changed_when):
                    intent.validation_errors = {"when": "unsupported_window"}
                    return collection_prompt(intent, language)
                changed_spec = resolve_request_when(changed_when, self.state.reference_time)
                clock = requested_clock(changed_when)
                if changed_spec.part_of_day:
                    values["part_of_day"] = changed_spec.part_of_day
                    if not clock and not (op.criteria and op.criteria.time_of_day):
                        values["time_of_day"] = None
                elif "part_of_day" not in op.clear_fields and values["part_of_day"] is None:
                    values["part_of_day"] = previous_spec.part_of_day
                if clock:
                    values["time_of_day"] = clock
                elif not changed_spec.part_of_day and "time_of_day" not in op.clear_fields and values["time_of_day"] is None:
                    values["time_of_day"] = requested_clock(previous_when)
                if (changed_spec.part_of_day or clock) and changed_spec.target_date is None and previous_spec.target_date:
                    values["when"] = previous_spec.target_date.isoformat()
            intent.criteria = SchedulingCriteria(**values)
            fields = intent.registration_fields.model_dump(exclude_none=True)
            for patch in (op.registration, op.registration_fields):
                if patch:
                    fields.update(patch.model_dump(exclude_none=True))
            intent.registration_fields = RegistrationFields(**fields)
            merged = op.model_copy(update={key: values[key] for key in ("specialty_id", "when", "doctor_name", "location_id", "appointment_id")})
            merged.criteria = intent.criteria
            merged.registration_fields = intent.registration_fields
            if intent.action != "register" and intent.patient is None:
                if intent.identity_status == "not_found":
                    intent.blocking_reason = "patient_not_found"
                return reason_text(intent.blocking_reason, language) if intent.blocking_reason else collection_prompt(intent, language)
            intent.offers, intent.blocking_reason = await self.clinic.prepare(self.state, intent, merged)
            intent.status = "awaiting_confirmation" if intent.offers else "blocked" if intent.blocking_reason else "collecting"
            if not intent.offers:
                return reason_text(intent.blocking_reason, language) if intent.blocking_reason else collection_prompt(intent, language)
            if any(offer.revision != intent.revision or offer.action != intent.action or offer.presented_turn is not None for offer in intent.offers):
                intent.offers = []
                intent.status = "blocked"
                raise ValueError("clinic produced an invalid offer")
            offered[intent.intent_id] = intent.revision
            self.store.event(self.state.run_id, "offers_prepared", {
                "intent_id": intent.intent_id, "revision": intent.revision,
                "offers": [offer.model_dump() for offer in intent.offers],
            })
            options = " ".join(self._describe_offer(intent, offer, index) for index, offer in enumerate(intent.offers, 1))
            return f"{options} {TEXT[language]['confirm']}"
        if op.op == "confirm":
            offer = self._confirmed_offer(intent, op, text)
            receipt = await self._submit(intent, offer.action, offer.payload)
            return TEXT[language]["received" if self.state.mode == "live" else "simulation"] if receipt["accepted"] else reason_text(None, language)
        if op.op in {"refuse", "escalate"}:
            self._evidence(op, text)
            reason = intent.blocking_reason
            if intent.action in {"no_action", "escalate"} and op.reason == "out_of_scope":
                reason = "out_of_scope"
            if not reason or (op.reason and op.reason != reason) or intent.offers:
                raise ValueError("a refusal needs a clinic-derived reason and no pending offer")
            if op.op == "refuse" and reason != "out_of_scope" and not explicit_decline(text):
                return reason_text(reason, language)
            action = "escalate" if op.op == "escalate" else "no_action"
            receipt = await self._submit(intent, action, {"reason": reason})
            return reason_text(reason if receipt["accepted"] else None, language)
        raise ValueError("operation not allowed in this state")

    def _expand_creates(self, operations: list[Operation], text: str = "") -> list[Operation]:
        """Complete the create, identify, prepare sequence the model often compresses.

        Models answer "I'm Rosa, born 7 August 1962, I want a GP" either with one create
        holding everything (create alone keeps none of it) or with no create at all, the
        action carried by another step. Only an action the decision states is ever used,
        and each added step passes through its own guards with the same evidence.
        """
        operations = self._implicit_creates(operations, text)
        creates = {op.intent_id: op for op in operations if op.op == "create" and op.action != "register"}
        expanded = []
        for op in operations:
            expanded.append(op)
            if op.op == "create" and op.intent_id in creates and op.identity is not None and not any(
                    other.op == "identify" and other.intent_id == op.intent_id for other in operations):
                expanded.append(Operation(op="identify", intent_id=op.intent_id, evidence=op.evidence, identity=op.identity))
        # Scheduling details the model put on create, identify or ask go through prepare, after identification.
        keys = ("specialty_id", "when", "doctor_name", "location_id", "appointment_id")
        for intent_id in dict.fromkeys(op.intent_id for op in operations if op.intent_id):
            own = [op for op in operations if op.intent_id == intent_id]
            # Only a request being set up; a confirm or refusal that repeats the criteria changes nothing.
            if any(op.op not in {"create", "identify", "ask"} for op in own):
                continue
            known = self.state.intents.get(intent_id)
            action = known.action if known else (creates[intent_id].action if intent_id in creates else None)
            if action is None or action == "register":
                continue
            scheduling = {key: next(getattr(op, key) for op in own if getattr(op, key) is not None)
                          for key in keys if any(getattr(op, key) is not None for op in own)}
            criteria = next((op.criteria for op in own if op.criteria is not None), None)
            if scheduling or criteria is not None:
                # The offers answer the model's own "which appointment?" question.
                expanded = [op for op in expanded if not (op.op == "ask" and op.intent_id == intent_id)]
                expanded.append(Operation(op="prepare", intent_id=intent_id, evidence=own[0].evidence,
                                          criteria=criteria, **scheduling))
        return expanded

    def _implicit_creates(self, operations: list[Operation], text: str = "") -> list[Operation]:
        stepping = {"identify", "prepare", "ask"}
        created = {op.intent_id for op in operations if op.op == "create"}
        unknown = [op for op in operations if op.op in stepping and op.intent_id
                   and op.intent_id not in self.state.intents and op.intent_id not in created and op.intent_id != "urgent"]
        if not unknown:
            return operations
        actions: dict[str, set] = {}
        for op in unknown:
            actions.setdefault(op.intent_id, set())
            if op.action:
                actions[op.intent_id].add(op.action)
        # No action stated at all: a named specialty or doctor with no existing appointment in play,
        # and no cancel/move wording, can only be a new booking. It still needs an accepted offer.
        if not any(actions.values()) and len(actions) == 1 and not self.state.intents:
            own = [op for op in operations if op.intent_id in actions]
            wants = any(op.specialty_id or op.doctor_name or (op.criteria and op.criteria.specialty_id) for op in own)
            existing = any(op.appointment_id or (op.criteria and (op.criteria.appointment_when or op.criteria.appointment_id))
                           for op in own)
            other = re.search(r"\b(cancel\w*|anul\w*|reschedul\w*|move|mover|cambi\w*|canvi\w*|moure|registr\w*|alta)\b",
                              normalize_text(text))
            if wants and not existing and not other:
                actions[next(iter(actions))].add("book")
        with_action = [key for key, found in actions.items() if len(found) == 1]
        without = [key for key, found in actions.items() if not found]
        if any(len(found) > 1 for found in actions.values()):
            return operations
        # One request split across two references: the action on one, the identity on the other.
        if len(with_action) == 1 and len(without) == 1 and not self.state.intents:
            target, stray = with_action[0], without[0]
            operations = [op.model_copy(update={"intent_id": target}) if op.intent_id == stray else op for op in operations]
            without = []
        if without:
            return operations
        creates = []
        for key in with_action:
            own = [op for op in operations if op.intent_id == key]
            identity = next((op.identity for op in own if op.identity is not None), None)
            creates.append(Operation(op="create", intent_id=key, action=next(iter(actions[key])),
                                     subject=(identity.name if identity and identity.name else "caller"),
                                     evidence=own[0].evidence))
        return creates + operations

    @staticmethod
    def _evidence(op: Operation, text: str):
        if not op.evidence.strip() or normalize_text(op.evidence) not in normalize_text(text):
            raise ValueError("operation needs evidence from this caller turn")

    def _describe_offer(self, intent: Intent, offer, option: int) -> str:
        d = offer.display
        language = self.state.language
        if intent.action == "register":
            values = ", ".join(f"{FIELDS[language].get(key, key)}: {value}" for key, value in d.items())
        else:
            values = ", ".join(spoken_when(str(d.get(key, "")), language) if key == "when" else str(d.get(key, ""))
                               for key in ("patient", "doctor", "site", "when"))
        verb = {"en": {"book": "Book", "cancel": "Cancel", "reschedule": "Move", "register": "Register"},
                "es": {"book": "Reservar", "cancel": "Cancelar", "reschedule": "Cambiar", "register": "Registrar"},
                "ca": {"book": "Reservar", "cancel": "Cancel·lar", "reschedule": "Canviar", "register": "Registrar"}}[language]
        prefix = {"en": "Option", "es": "Opción", "ca": "Opció"}[language]
        explanation = ""
        if d.get("alternative"):
            label = {"en": "Alternative, not the requested date or time", "es": "Alternativa, no es la fecha u hora solicitada",
                     "ca": "Alternativa, no és la data o hora sol·licitada"}[language]
            explanation = reason_text(d.get("alternative_reason") or "no_availability", language) + " " + label + ". "
        if d.get("previous"):
            old = ", ".join(spoken_when(str(d["previous"].get(key) or ""), language) if key == "when"
                            else str(d["previous"].get(key) or "") for key in ("doctor", "site", "when"))
            values = {"en": f"from {old} to {values}", "es": f"de {old} a {values}", "ca": f"de {old} a {values}"}[language]
        if d.get("policy"):
            values += {"en": ", using plan ", "es": ", con el seguro ", "ca": ", amb l'assegurança "}[language] + d["policy"]
        return f"{explanation}{prefix} {option}: {verb[intent.action]} {values}."

    def presented(self, reply: Reply, *, interrupted: bool = False):
        if interrupted or reply.epoch != self.epoch or reply != self.pending_reply:
            return
        for intent_id, revision in reply.offers.items():
            intent = self.state.intents[intent_id]
            if intent.revision == revision:
                for offer in intent.offers:
                    offer.presented_turn = self.state.turn
        self.pending_reply = None
        self.state.history = [*self.state.history[-7:], {"role": "agent", "text": reply.text}]
        self.store.event(self.state.run_id, "response_presented", {"response_id": reply.response_id,
                                                                  "epoch": reply.epoch})
        self.store.save(self.state)
