from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from uuid import uuid4

from v2.clinic import Dispatcher, FixtureClinic
from v2.config import Config
from v2.models import CallState, Operation, RehearsalRequest, RehearsalTurn, TurnDecision
from v2.store import RunStore
from v2.workflow import CallController


def demo_request(language: str = "en") -> RehearsalRequest:
    text = {
        "en": ["Please cancel my appointment. I am Lina Demo, born 1990-01-01.",
               "Yes, option one. Also book for Roc Demo, born 2018-01-01.", "Yes, option one.", "Thank you. That is everything."],
        "es": ["Quiero cancelar mi cita. Soy Lina Demo, nací el 1990-01-01.",
               "Sí, la primera opción. También quiero cita para Roc Demo, nacido el 2018-01-01.",
               "Sí, la primera opción.", "Gracias, eso es todo."],
        "ca": ["Vull cancel·lar la meva cita. Soc Lina Demo, vaig néixer el 1990-01-01.",
               "Sí, la primera opció. També vull una cita per a Roc Demo, nascut el 2018-01-01.",
               "Sí, la primera opció.", "Gràcies, això és tot."],
    }[language]
    yes = "Yes" if language == "en" else "Sí"
    def turn(text, *ops):
        return RehearsalTurn(text=text, decision=TurnDecision(language=language, operations=[Operation(**op) for op in ops]))
    return RehearsalRequest(language=language, turns=[
        turn(text[0],
             {"op": "create", "intent_id": "mine", "action": "cancel", "subject": "Lina Demo", "evidence": "Lina Demo"},
             {"op": "identify", "intent_id": "mine", "identity": {"name": "Lina Demo", "date_of_birth": "1990-01-01"}, "evidence": "Lina Demo"},
             {"op": "prepare", "intent_id": "mine", "evidence": "Lina Demo"}),
        turn(text[1],
             {"op": "confirm", "intent_id": "mine", "option": 1, "offer_revision": 1, "evidence": yes},
             {"op": "create", "intent_id": "child", "action": "book", "subject": "Roc Demo", "evidence": "Roc Demo"},
             {"op": "identify", "intent_id": "child", "identity": {"name": "Roc Demo", "date_of_birth": "2018-01-01"}, "evidence": "Roc Demo"},
             {"op": "prepare", "intent_id": "child", "specialty_id": "paediatrics", "when": "tomorrow", "evidence": "Roc Demo"}),
        turn(text[2], {"op": "confirm", "intent_id": "child", "option": 1, "offer_revision": 1, "evidence": yes}),
        turn(text[3], {"op": "finish"}),
    ])


def metrics(report: dict) -> dict:
    events = report["events"]
    receipts = [event["payload"] for event in events if event["kind"] == "action_receipt"]
    responses = [event["payload"]["elapsed_ms"] for event in events if event["kind"] == "response_planned"]
    audio = [event["payload"] for event in events if event["kind"] == "audio_output"]
    intents = list(report["state"]["intents"].values())
    return {"intents": len(intents), "resolved": sum(i["status"] == "completed" for i in intents),
            "missing_submission": not bool(receipts),
            "http_accepted": sum(r["source"] == "platform_receipt" and r["accepted"] for r in receipts),
            "simulated_receipts": sum(r["source"] == "simulation" for r in receipts),
            "audio_status": audio[-1]["audio_status"] if audio else "not_measured",
            "response_ms": responses,
            "guard_rejections": sum(e["kind"] == "guard_rejected" for e in events),
            "errors": sum(e["kind"] in {"error", "provider_error", "pipeline_error"} for e in events),
            "official_grade": None}


def fixture_grade(report: dict, expected: list[tuple[str, str]]) -> dict:
    receipts = [e["payload"] for e in report["events"] if e["kind"] == "action_receipt" and e["payload"]["accepted"]]
    actual = [(r["action"], r["payload"].get("patient_id") or r["payload"].get("appointment_id", "")) for r in receipts]
    resolved = bool(report["state"]["intents"]) and all(i["status"] == "completed" for i in report["state"]["intents"].values())
    return {"source": "fixture", "rubric": "demo-v1", "passed": resolved and sorted(actual) == sorted(expected),
            "expected": expected, "actual": actual, "official_grade": None}


async def rehearse(request: RehearsalRequest, store: RunStore, manifest: dict | None = None) -> dict:
    state = CallState(call_id="rehearsal-" + uuid4().hex, language=request.language,
                      reference_time=datetime.fromisoformat("2026-09-19T09:00:00+02:00"))
    controller = CallController(state, FixtureClinic(), store, Dispatcher(store, "simulation"),
                                manifest={**(manifest or {}), "execution": "scripted_fixture", "fixture": "clinic-demo-v1"})
    for item in request.turns:
        reply = await controller.turn(item.text, item.decision)
        if reply:
            controller.presented(reply)
    report = store.report(state.run_id)
    return {**report, "metrics": metrics(report)}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run a synthetic v2 regression. Offline by default; --duel and --review spend API credits.")
    parser.add_argument("--database", default=None)
    parser.add_argument("--duel", action="store_true", help="two-model text conversation against fixtures; never platform submissions")
    parser.add_argument("--review", action="store_true", help="send this synthetic run to the configured Jev adapter")
    parser.add_argument("--language", choices=["en", "es", "ca"], default="en")
    args = parser.parse_args()
    config = Config()
    if (args.duel or args.review) and not config.allow_paid:
        parser.error("paid experiments require V2_ENABLE_PAID=true")
    if args.duel and not config.gateway_key:
        parser.error("AI_GATEWAY_API_KEY is required for --duel")
    if args.review and (not config.jev_url or not config.jev_token):
        parser.error("V2_JEV_URL and V2_JEV_TOKEN are required for --review")
    if (args.duel or args.review) and args.database is not None:
        parser.error("paid experiments use the persistent project ledger; --database is offline-only")
    store = RunStore(args.database or str(config.data_dir / "runs.db"))
    try:
        if args.duel:
            from v2.simulator import run_duel
            report = await run_duel(config, store, args.language)
            grade = report["fixture_grade"]
        else:
            report = await rehearse(demo_request(args.language), store, config.manifest())
            grade = fixture_grade(report, [("cancel", "fixture-appointment-fixture-adult"), ("book", "fixture-child")])
            store.event(report["run_id"], "fixture_grade", grade)
        if args.review:
            from v2.providers import JevClient
            client = JevClient(config, store)
            try:
                turns = [{"role": "caller" if e["kind"] == "caller_turn" else "agent", "text": e["payload"]["text"]}
                         for e in report["events"] if e["kind"] in {"caller_turn", "response_planned"}]
                await client.evaluate(report["run_id"], turns)
            finally:
                await client.close()
        print(f"run_id={report['run_id']}")
        print(f"fixture_pass={grade['passed']} resolved={report['metrics']['resolved']}/{report['metrics']['intents']}")
        print("official_grade=unknown; audio=not_measured")
        print(json.dumps(store.budget()))
        return 0 if grade["passed"] else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
