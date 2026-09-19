from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from v2.models import Operation, RehearsalRequest, RehearsalTurn, TurnDecision

VERSION = "six-outcomes-v1"
CLOCK = "2026-09-19T09:00:00+02:00"
LANGUAGES = ("en", "es", "ca")
OUTCOMES = ("book", "cancel", "reschedule", "register", "no_action", "escalate")
REGISTRATION = {
    "given_name": "Ada", "first_surname": "Demo", "second_surname": "Ficticia",
    "national_id": "00000000T", "date_of_birth": "1990-01-01", "phone": "600000000",
    "email": "ada.demo@example.invalid", "insurer": "sanitas",
}
TEXTS = {
    "en": {
        "book": "Please book general practice tomorrow for Lina Demo, born 1990-01-01.",
        "cancel": "Please cancel my appointment. I am Lina Demo, born 1990-01-01.",
        "reschedule": "Please move my appointment to tomorrow. I am Lina Demo, born 1990-01-01.",
        "register": "Please register me: Ada Demo Ficticia, 00000000T, born 1990-01-01, phone 600000000, ada.demo@example.invalid, Sanitas.",
        "no_action": "Please order a pizza for me.",
        "escalate": "I have chest pain and cannot breathe.",
        "yes": "Yes, option one.", "finish": "Thank you. That is everything.",
    },
    "es": {
        "book": "Quiero una cita de medicina general mañana para Lina Demo, nacida el 1990-01-01.",
        "cancel": "Quiero cancelar mi cita. Soy Lina Demo, nací el 1990-01-01.",
        "reschedule": "Quiero cambiar mi cita a mañana. Soy Lina Demo, nací el 1990-01-01.",
        "register": "Quiero registrarme: Ada Demo Ficticia, 00000000T, nacida el 1990-01-01, teléfono 600000000, ada.demo@example.invalid, Sanitas.",
        "no_action": "Quiero pedir una pizza, por favor.",
        "escalate": "Tengo dolor en el pecho y no puedo respirar.",
        "yes": "Sí, la primera opción.", "finish": "Gracias, eso es todo.",
    },
    "ca": {
        "book": "Vull una cita de medicina general demà per a Lina Demo, nascuda el 1990-01-01.",
        "cancel": "Vull cancel·lar la meva cita. Soc Lina Demo, vaig néixer el 1990-01-01.",
        "reschedule": "Vull canviar la meva cita a demà. Soc Lina Demo, vaig néixer el 1990-01-01.",
        "register": "Vull registrar-me: Ada Demo Ficticia, 00000000T, nascuda el 1990-01-01, telèfon 600000000, ada.demo@example.invalid, Sanitas.",
        "no_action": "Vull demanar una pizza, si us plau.",
        "escalate": "Tinc dolor al pit i no puc respirar.",
        "yes": "Sí, la primera opció.", "finish": "Gràcies, això és tot.",
    },
}
GOALS = {
    "book": "You are Lina Demo, born 1990-01-01. Ask for a general practice appointment tomorrow. Accept an appropriate offered time only after hearing it.",
    "cancel": "You are Lina Demo, born 1990-01-01. Cancel your existing appointment, confirming it only after the receptionist identifies it.",
    "reschedule": "You are Lina Demo, born 1990-01-01. Move your existing appointment to tomorrow, confirming only an option you have heard.",
    "register": "You want to register as Ada Demo Ficticia, national ID 00000000T, born 1990-01-01, phone 600000000, email ada.demo@example.invalid, insurer Sanitas. Confirm only after the receptionist reads the details back.",
    "no_action": "You want to order a pizza. If the receptionist explains that the clinic cannot do this, accept the explanation and end the call.",
    "escalate": "You have chest pain and cannot breathe. Describe this immediately. Accept urgent-care guidance rather than seeking a routine appointment.",
    "multi_intent": "You are Lina Demo, born 1990-01-01. Cancel your own existing appointment and book paediatrics tomorrow for your child Roc Demo, born 2018-01-01. Accept appropriate options only after hearing them. End only after both requests are addressed or the receptionist cannot continue.",
}


def _validate(outcome, language):
    if outcome not in OUTCOMES or language not in LANGUAGES:
        raise ValueError("unsupported outcome fixture or language")


def outcome_request(outcome: str, language: str = "en") -> RehearsalRequest:
    _validate(outcome, language)
    words = TEXTS[language]
    text = words[outcome]
    def turn(spoken, operations):
        return RehearsalTurn(text=spoken, decision=TurnDecision(language=language, operations=[Operation(**op) for op in operations]))
    if outcome == "escalate":
        return RehearsalRequest(language=language, turns=[turn(text, [])])
    operations = [{"op": "create", "intent_id": "request", "action": outcome,
                   "subject": "Ada Demo Ficticia" if outcome == "register" else ("caller" if outcome == "no_action" else "Lina Demo"),
                   "evidence": text}]
    if outcome == "no_action":
        operations.append({"op": "refuse", "intent_id": "request", "reason": "out_of_scope", "evidence": text})
        return RehearsalRequest(language=language, turns=[turn(text, operations), turn(words["finish"], [{"op": "finish"}])])
    if outcome == "register":
        operations.append({"op": "prepare", "intent_id": "request", "registration": deepcopy(REGISTRATION), "evidence": text})
    else:
        operations.extend([
            {"op": "identify", "intent_id": "request", "identity": {"name": "Lina Demo", "date_of_birth": "1990-01-01"}, "evidence": text},
            {"op": "prepare", "intent_id": "request", "specialty_id": "general_practice", "when": "tomorrow", "evidence": text},
        ])
    return RehearsalRequest(language=language, turns=[
        turn(text, operations),
        turn(words["yes"], [{"op": "confirm", "intent_id": "request", "option": 1, "offer_revision": 1, "evidence": words["yes"]}]),
        turn(words["finish"], [{"op": "finish"}]),
    ])


def outcome_oracle(outcome: str) -> list[dict]:
    if outcome not in OUTCOMES:
        raise ValueError("unsupported outcome oracle")
    common = {"provider_id": "fixture-provider", "location_id": "centro", "slot": "2026-09-21T10:00:00+02:00", "policy_id": "fixture-policy"}
    payloads = {
        "book": {**common, "patient_id": "fixture-adult", "appointment_type_id": "fixture-type"},
        "cancel": {"appointment_id": "fixture-appointment-fixture-adult"},
        "reschedule": {**common, "appointment_id": "fixture-appointment-fixture-adult"},
        "register": deepcopy(REGISTRATION), "no_action": {"reason": "out_of_scope"},
        "escalate": {"reason": "medical_emergency"},
    }
    return [{"action": outcome, "payload": payloads[outcome]}]


def scenario_oracle(scenario: str) -> list[dict]:
    if scenario == "multi_intent":
        expected = outcome_oracle("cancel") + outcome_oracle("book")
        expected[1]["payload"]["patient_id"] = "fixture-child"
        return expected
    return outcome_oracle(scenario)


def caller_goal(scenario: str = "multi_intent") -> str:
    if not isinstance(scenario, str) or scenario not in GOALS:
        raise ValueError("unsupported caller scenario")
    return GOALS[scenario]


def fixture_provenance(outcome: str, language: str, split: str = "regression") -> dict:
    _validate(outcome, language)
    if split not in {"regression", "holdout"}:
        raise ValueError("invalid fixture split")
    data = {"request": outcome_request(outcome, language).model_dump(mode="json"), "oracle": outcome_oracle(outcome),
            "clock": CLOCK, "version": VERSION}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"fixture": f"{VERSION}/{outcome}/{language}", "fixture_sha256": digest,
            "fixture_version": VERSION, "reference_time": CLOCK, "dataset_split": split,
            "synthetic": True, "noise_version": None, "audio_fixture": False}
