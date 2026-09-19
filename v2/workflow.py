from __future__ import annotations

import asyncio
import re
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith.run_helpers import tracing_context

from v2.domain.identity import normalize_text
from v2.domain.triage import triage
from v2.language import decide_language, should_apply_language
from v2.clinic import Dispatcher
from v2.models import CallState, Intent, Operation, Reply, TurnDecision
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
        "hello": "Clínica Arenal. How can I help you?",
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
    folded = normalize_text(text)
    if re.search(r"\b(no|not|wait|but|pero|per[oò]|instead|espera|except)\b", folded):
        return False
    return bool(re.match(r"^(yes|si|okay|ok|correct|confirmo|confirm|d'acord|perfecte|adelante|endavant)\b", folded))


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
        if self.interpret_task is not None:
            self.interpret_task.cancel()
        self.store.event(self.state.run_id, "interruption" if source == "caller" else "response_cancelled",
                         {"epoch": self.epoch, "source": source})

    async def turn(self, text: str, decision: TurnDecision | None = None, *, expected_epoch: int | None = None) -> Reply | None:
        if not text.strip() or len(text) > 2000:
            raise ValueError("turn must contain between 1 and 2000 characters")
        async with self.lock:
            if expected_epoch is not None and expected_epoch != self.epoch:
                return None
            self.state.history = [*self.state.history[-7:], {"role": "caller", "text": text}]
            self.state.turn += 1
            if self.state.turn > 20:
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
            except asyncio.CancelledError:
                if epoch != self.epoch and not asyncio.current_task().cancelling():
                    return None
                raise
            except Exception as exc:
                self.store.event(self.state.run_id, "error", {"where": "turn", "error_type": type(exc).__name__})
                result = {"reply": Reply(text=TEXT[self.state.language]["clarify"],
                                         language=self.state.language, epoch=epoch)}
            finally:
                self.interpret_task = None
                self.state.revision += 1
                self.store.save(self.state)
            if epoch != self.epoch:
                return None
            reply = result["reply"]
            self.pending_reply = reply
            self.store.event(self.state.run_id, "response_planned", {
                **reply.model_dump(), "elapsed_ms": round((time.monotonic() - started) * 1000),
            })
            return reply

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
        detected = decide_language(text, decision.language, self.state.language, established=self.state.turn > 1)
        if decision.language == "ca" and re.search(r"\b(vull|voldria|puc|meva|meu|bon dia|si us plau)\b", normalize_text(text)):
            self.state.language = "ca"
        elif should_apply_language(self.state.language, self.state.turn > 1, detected):
            self.state.language = detected.code
        language = self.state.language
        lines, offered = [], {}
        red_flag = triage(text).escalate or bool(CA_URGENT.search(normalize_text(text)))
        proposed_escalation = any(op.op == "escalate" and op.reason == "medical_emergency"
                                 and op.evidence.strip() and normalize_text(op.evidence) in normalize_text(text)
                                 for op in decision.operations)
        if red_flag or self.state.emergency or proposed_escalation:
            self.state.emergency = True
            intent = self.state.intents.setdefault("urgent", Intent(intent_id="urgent", action="escalate", subject="caller"))
            receipt = await self.dispatcher.execute(self.state, intent, "escalate", {"reason": "medical_emergency"})
            intent.receipts.append(receipt)
            intent.status = "completed" if receipt["accepted"] else "blocked"
            self.state.completion_requested = receipt["accepted"]
            return {"reply": Reply(text=TEXT[language]["emergency"], language=language, epoch=flow["epoch"],
                                    completion=receipt["accepted"])}
        confirms = [op for op in decision.operations if op.op == "confirm"]
        if len(confirms) > 1 and not re.search(r"\b(both|all|ambdues|totes|dos|dues)\b", normalize_text(text)):
            raise ValueError("multiple confirmations need explicit collective acceptance")
        for op in decision.operations:
            if flow["epoch"] != self.epoch:
                break
            try:
                line = await self._operate(op, text, offered)
                if line:
                    lines.append(line)
            except (ValueError, PermissionError, RuntimeError) as exc:
                self.store.event(self.state.run_id, "guard_rejected", {"operation": op.op,
                                                                       "error_type": type(exc).__name__})
                lines.append(TEXT[language]["clarify"])
                self.state.completion_requested = False
                break
        return {"reply": Reply(text=" ".join(lines) or TEXT[language]["identity"], language=language,
                                epoch=flow["epoch"], offers=offered,
                                completion=self.state.completion_requested)}

    async def _operate(self, op: Operation, text: str, offered: dict) -> str | None:
        language = self.state.language
        if op.op == "finish":
            self.state.completion_requested = self.state.all_resolved
            return TEXT[language]["done" if self.state.all_resolved else "remaining"]
        if op.op == "ask":
            return TEXT[language][op.question or "clarify"]
        if op.op == "facts":
            facts = self.clinic.facts(op.topic or "sites", language, op.location_id)
            self.store.event(self.state.run_id, "clinic_facts", facts)
            return facts["text"]
        if not op.intent_id:
            raise ValueError("intent reference required")
        if op.op == "create":
            if not op.action or not op.subject or op.intent_id in self.state.intents:
                raise ValueError("a new intent needs an action and a unique reference")
            self._evidence(op, text)
            self.state.intents[op.intent_id] = Intent(intent_id=op.intent_id, action=op.action, subject=op.subject)
            return None
        intent = self.state.intents.get(op.intent_id)
        if intent is None or intent.status == "completed":
            raise ValueError("unknown or already resolved intent")
        if op.op == "identify":
            self._evidence(op, text)
            if op.identity is None:
                raise ValueError("identity fields required")
            if intent.offers:
                intent.revision += 1
            intent.offers = []
            intent.identity_inputs = op.identity.model_dump(exclude_none=True)
            intent.patient = await self.clinic.identify(op.identity)
            intent.status = "collecting"
            return None if intent.patient else TEXT[language]["identity"]
        if op.op == "prepare":
            self._evidence(op, text)
            intent.revision += 1
            intent.offers = []
            intent.offers, intent.blocking_reason = await self.clinic.prepare(self.state, intent, op)
            intent.status = "awaiting_confirmation" if intent.offers else "blocked"
            if not intent.offers:
                return TEXT[language]["empty"] if intent.blocking_reason else TEXT[language]["clarify"]
            offered[intent.intent_id] = intent.revision
            self.store.event(self.state.run_id, "offers_prepared", {
                "intent_id": intent.intent_id, "revision": intent.revision,
                "offers": [offer.model_dump() for offer in intent.offers],
            })
            return " ".join(self._describe_offer(intent, offer, index) for index, offer in enumerate(intent.offers, 1))
        if op.op == "confirm":
            self._evidence(op, text)
            if not explicit_acceptance(text) or op.offer_revision != intent.revision or not op.option:
                raise ValueError("explicit acceptance of the current offer is required")
            if op.option > len(intent.offers):
                raise ValueError("unknown option")
            offer = intent.offers[op.option - 1]
            if offer.presented_turn is None or offer.presented_turn >= self.state.turn:
                raise ValueError("the offer has not been presented to the caller")
            receipt = await self.dispatcher.execute(self.state, intent, offer.action, offer.payload)
            intent.receipts.append(receipt)
            intent.status = "completed" if receipt["accepted"] else "blocked"
            return TEXT[language]["received" if self.state.mode == "live" else "simulation"] if receipt["accepted"] else TEXT[language]["clarify"]
        if op.op == "refuse":
            reason = intent.blocking_reason
            if intent.action == "no_action" and op.reason == "out_of_scope":
                reason = "out_of_scope"
            if not reason or (op.reason and op.reason != reason):
                raise ValueError("a refusal needs a clinic-derived reason")
            receipt = await self.dispatcher.execute(self.state, intent, "no_action", {"reason": reason})
            intent.receipts.append(receipt)
            intent.status = "completed" if receipt["accepted"] else "blocked"
            return TEXT[language]["empty"]
        raise ValueError("operation not allowed in this state")

    @staticmethod
    def _evidence(op: Operation, text: str):
        if not op.evidence.strip() or normalize_text(op.evidence) not in normalize_text(text):
            raise ValueError("operation needs evidence from this caller turn")

    def _describe_offer(self, intent: Intent, offer, option: int) -> str:
        d = offer.display
        language = self.state.language
        if intent.action == "register":
            values = ", ".join(str(v) for v in d.values())
        else:
            values = ", ".join(str(d.get(key, "")) for key in ("patient", "doctor", "site", "when"))
        verb = {"en": {"book": "Book", "cancel": "Cancel", "reschedule": "Move", "register": "Register"},
                "es": {"book": "Reservar", "cancel": "Cancelar", "reschedule": "Cambiar", "register": "Registrar"},
                "ca": {"book": "Reservar", "cancel": "Cancel·lar", "reschedule": "Canviar", "register": "Registrar"}}[language]
        prefix = {"en": "Option", "es": "Opción", "ca": "Opció"}[language]
        return f"{prefix} {option}: {verb[intent.action]} {values}. {TEXT[language]['confirm']}"

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
