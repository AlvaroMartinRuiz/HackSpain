from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from v2.clinic import Dispatcher, FixtureClinic
from v2.config import Config
from v2.models import CallState, Operation, RehearsalRequest, RehearsalTurn, TurnDecision
from v2.store import RunStore, report_metrics
from v2.fixtures.outcomes import CLOCK, OUTCOMES, fixture_provenance, outcome_oracle, outcome_request
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
    return report_metrics(report)


def _receipts(report):
    receipts = {}
    for index, event in enumerate(report.get("events", [])):
        if event.get("kind") == "action_receipt":
            receipt = event.get("payload", {})
            receipts[receipt.get("action_key") or event.get("event_id", str(index))] = receipt
    return list(receipts.values())


def fixture_grade(report: dict, expected: list, *, rubric: str = "demo-v1") -> dict:
    if not isinstance(expected, list) or not expected or len(expected) > 20:
        raise ValueError("a bounded, nonempty grading oracle is required")
    structured = all(isinstance(item, dict) and set(item) == {"action", "payload"} and
                     isinstance(item["action"], str) and isinstance(item["payload"], dict) for item in expected)
    pairs = all(isinstance(item, (tuple, list)) and len(item) == 2 and all(isinstance(v, str) for v in item) for item in expected)
    if not structured and not pairs:
        raise ValueError("oracle must contain action/payload objects or action/entity pairs")
    receipts = _receipts(report)
    accepted = [r for r in receipts if r.get("accepted") is True]
    if structured:
        actual = [{"action": r.get("action"), "payload": {k: v for k, v in r.get("payload", {}).items() if k != "call_id"}} for r in accepted]
    else:
        actual = [(r.get("action"), r.get("payload", {}).get("patient_id") or r.get("payload", {}).get("appointment_id", "")) for r in accepted]
    canonical = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    matched = sorted(map(canonical, actual)) == sorted(map(canonical, expected))
    intents = list(report.get("state", {}).get("intents", {}).values())
    resolved = bool(intents) and all(i.get("status") == "completed" for i in intents)
    unresolved = any(a.get("status") not in {"accepted", "rejected"} for a in report.get("actions", []))
    invalid = report.get("status") in {"error", "timed_out"} or any(e.get("kind") in {"error", "provider_error", "pipeline_error"} for e in report.get("events", []))
    rejected = any(r.get("accepted") is not True for r in receipts)
    source_ok = report.get("mode") == "simulation" and all(r.get("source") == "simulation" for r in accepted)
    return {"source": "fixture", "rubric": rubric, "passed": resolved and matched and not (unresolved or invalid or rejected) and source_ok,
            "expected": expected, "actual": actual, "all_intents_completed": resolved,
            "receipt_match": matched, "unresolved_actions": unresolved,
            "execution": report.get("manifest", {}).get("execution", "unknown"),
            "oracle_sha256": hashlib.sha256(canonical(expected).encode()).hexdigest(), "official_grade": None}


def failure_review(report: dict, grade: dict | None = None) -> dict:
    events = report.get("events", [])
    categories = []
    experiments = {
        "provider_failure": "Replay the failing provider stage with a sanitized mocked response before another paid probe.",
        "invalid_data": "Replay schema validation with a bounded malformed provider or scenario payload.",
        "limit_exhausted": "Inspect turn progression and repeat with the same fixed limit; do not silently extend spend.",
        "unresolved_action": "Reconcile the existing action with provider evidence; do not resubmit it.",
        "guard_rejection": "Check current identity, offer revision and presentation evidence using the same fixture.",
        "semantic_mismatch": "Compare the recorded action payload to the independent oracle, including patient and time.",
        "unresolved_intents": "Replay the unanswered request with the same language and caller goal.",
        "audio_failure": "Use offline socket-send telemetry fixtures; do not infer intelligibility from text.",
    }
    if any(e["kind"] == "provider_error" for e in events):
        categories.append("provider_failure")
    if any(e["kind"] in {"error", "provider_error"} and e.get("payload", {}).get("error_type", e.get("payload", {}).get("type")) in {"ValueError", "ValidationError", "JSONDecodeError", "KeyError", "IndexError", "TypeError", "AttributeError"} for e in events):
        categories.append("invalid_data")
    if any(e["kind"] == "simulation_ended" and e.get("payload", {}).get("reason") in {"turn_limit", "time_limit"} for e in events):
        categories.append("limit_exhausted")
    if any(a.get("status") in {"pending", "unresolved"} for a in report.get("actions", [])):
        categories.append("unresolved_action")
    measured = metrics(report)
    if measured["guard_rejections"]:
        categories.append("guard_rejection")
    if grade and not grade.get("receipt_match", grade.get("passed")):
        categories.append("semantic_mismatch")
    if measured["all_intents_completed"] is not True:
        categories.append("unresolved_intents")
    if measured["audio_status"] == "silent":
        categories.append("audio_failure")
    if not categories and measured["errors"]:
        categories.append("invalid_data")
    evidence = [{"event_id": e.get("event_id"), "sequence": e.get("sequence"), "kind": e["kind"]} for e in events
                if e["kind"] in {"error", "provider_error", "pipeline_error", "guard_rejected", "action_receipt", "simulation_ended", "audio_output"}][-40:]
    return {"source": "deterministic_failure_review", "rubric": "failure-review-v1", "run_id": report.get("run_id"),
            "categories": categories, "primary_category": categories[0] if categories else ("no_failure_observed" if grade and grade.get("passed") else "not_evaluated"),
            "evidence": evidence, "suggested_experiments": [experiments[c] for c in categories],
            "affected_cases": [report.get("manifest", {}).get("fixture") or report.get("manifest", {}).get("scenario")],
            "official_grade": None, "automatic_changes": False}


def evaluation_provenance(manifest: dict | None = None) -> dict:
    source = {**Config().manifest(), **(manifest or {})}
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("fixtures/*.py")):
        digest.update(path.name.encode() + path.read_bytes())
    return {**source, "evaluation_schema": 2, "evaluation_rubric": "six-outcomes-v1",
            "fixture_code_sha256": digest.hexdigest(), "reference_time": CLOCK,
            "synthetic": True, "dataset_split": source.get("dataset_split", "regression"),
            "audio_fixture": False, "noise_version": None}


def save_evaluation(store: RunStore, report: dict, expected: list, *, rubric="demo-v1") -> dict:
    grade = fixture_grade(report, expected, rubric=rubric)
    review = failure_review(report, grade)
    store.event(report["run_id"], "fixture_grade", json.loads(json.dumps(grade)))
    store.event(report["run_id"], "failure_review", review)
    store.event(report["run_id"], "evaluation_summary", {
        "source": "local_evaluation", "execution": report["manifest"].get("execution"),
        "fixture": report["manifest"].get("fixture"), "scenario": report["manifest"].get("scenario"),
        "dataset_split": report["manifest"].get("dataset_split"), "rubric": rubric,
        "oracle_sha256": grade["oracle_sha256"], "passed": grade["passed"],
        "failure_categories": review["categories"], "official_grade": None,
    })
    fresh = store.report(report["run_id"])
    return {**fresh, "metrics": metrics(fresh), "fixture_grade": grade, "failure_review": review}


async def rehearse(request: RehearsalRequest, store: RunStore, manifest: dict | None = None) -> dict:
    request = RehearsalRequest.model_validate(request.model_dump())
    request_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    provenance = evaluation_provenance(manifest)
    default_fixture = "clinic-demo-v1" if request == demo_request(request.language) else "operator-script-v1"
    state = CallState(call_id="rehearsal-" + uuid4().hex, language=request.language,
                      reference_time=datetime.fromisoformat(CLOCK))
    controller = CallController(state, FixtureClinic(), store, Dispatcher(store, "simulation"),
                                manifest={**provenance, "execution": "scripted_fixture",
                                          "fixture": provenance.get("fixture", default_fixture), "request_sha256": request_hash})
    store.event(state.run_id, "session_started", {"status": "active", "transport": "text"})
    status = "disconnected"
    try:
        for item in request.turns:
            reply = await controller.turn(item.text, item.decision)
            if reply:
                controller.presented(reply)
        status = "completed" if state.all_resolved and state.completion_requested else "disconnected"
    except Exception as exc:
        store.event(state.run_id, "error", {"where": "rehearsal", "error_type": type(exc).__name__})
        status = "error"
    finally:
        store.save(state)
        store.finish_run(state.run_id, status, reason="script_exhausted")
    report = store.report(state.run_id)
    if default_fixture == "clinic-demo-v1":
        return save_evaluation(store, report, [("cancel", "fixture-appointment-fixture-adult"), ("book", "fixture-child")])
    store.event(state.run_id, "failure_review", failure_review(report))
    fresh = store.report(state.run_id)
    return {**fresh, "metrics": metrics(fresh)}


async def evaluate_outcome(outcome: str, language: str, store: RunStore, *, split="regression", manifest=None) -> dict:
    provenance = fixture_provenance(outcome, language, split)
    report = await rehearse(outcome_request(outcome, language), store, {**evaluation_provenance(manifest), **provenance})
    return save_evaluation(store, report, outcome_oracle(outcome), rubric="six-outcomes-v1")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run a synthetic v2 regression. Offline by default; --duel and --review spend API credits.")
    parser.add_argument("--database", default=None)
    parser.add_argument("--duel", action="store_true", help="two-model text conversation against fixtures; never platform submissions")
    parser.add_argument("--review", action="store_true", help="send this synthetic run to the configured Jev adapter")
    parser.add_argument("--language", choices=["en", "es", "ca"], default="en")
    parser.add_argument("--outcome", choices=["demo", *OUTCOMES], default="demo")
    parser.add_argument("--suite", action="store_true", help="offline six-outcome regression in all three languages")
    args = parser.parse_args()
    if args.suite and (args.duel or args.review):
        parser.error("the offline suite cannot use paid providers")
    config = Config()
    if (args.duel or args.review) and not config.allow_paid:
        parser.error("paid experiments require V2_ENABLE_PAID=true")
    if args.duel and not config.gateway_key:
        parser.error("AI_GATEWAY_API_KEY is required for --duel")
    if args.review and (not config.jev_url or not config.jev_token):
        parser.error("V2_JEV_URL and V2_JEV_TOKEN are required for --review")
    if (args.duel or args.review) and args.database is not None:
        parser.error("paid experiments use the project's persistent run store; --database is offline-only")
    store = RunStore(args.database or str(config.data_dir / "runs.db"))
    try:
        if args.suite:
            results = []
            for language in ("en", "es", "ca"):
                for outcome in OUTCOMES:
                    report = await evaluate_outcome(outcome, language, store, manifest=config.manifest())
                    results.append({"run_id": report["run_id"], "outcome": outcome, "language": language,
                                    "passed": report["fixture_grade"]["passed"], "source": "scripted_fixture",
                                    "failure_categories": report["failure_review"]["categories"], "official_grade": None})
            print(json.dumps({"results": results}))
            return 0 if all(r["passed"] for r in results) else 1
        if args.duel:
            from v2.simulator import run_duel
            report = await run_duel(config, store, args.language, scenario="multi_intent" if args.outcome == "demo" else args.outcome)
        elif args.outcome != "demo":
            report = await evaluate_outcome(args.outcome, args.language, store, manifest=config.manifest())
        else:
            report = await rehearse(demo_request(args.language), store, {**config.manifest(), "fixture": "clinic-demo-v1"})
        grade = report["fixture_grade"]
        review_failed = False
        if args.review:
            from v2.providers import JevClient
            client = JevClient(config, store)
            try:
                turns = [{"role": "caller" if e["kind"] == "caller_turn" else "agent", "text": e["payload"]["text"]}
                         for e in report["events"] if e["kind"] in {"caller_turn", "response_planned"}]
                await client.evaluate(report["run_id"], turns)
            except Exception as exc:
                review_failed = True
                store.event(report["run_id"], "provider_error", {"provider": "jev", "phase": "evaluation", "error_type": type(exc).__name__})
                store.event(report["run_id"], "failure_review", failure_review(store.report(report["run_id"]), grade))
            finally:
                await client.close()
        print(f"run_id={report['run_id']}")
        print(f"fixture_pass={grade['passed']} resolved={report['metrics']['resolved']}/{report['metrics']['intents']}")
        print("official_grade=unknown; audio=not_measured")
        return 0 if grade["passed"] and not review_failed else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
