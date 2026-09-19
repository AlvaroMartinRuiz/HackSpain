from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

import httpx
from pydantic import Field

from v2.clinic import Dispatcher, FixtureClinic
from v2.config import Config
from v2.evaluation import fixture_grade, metrics
from v2.models import CallState, Model
from v2.providers import VercelInterpreter
from v2.store import RunStore
from v2.workflow import CallController, TEXT

GOAL = """You are a synthetic caller, not the receptionist. Your name is Lina Demo, born 1990-01-01.
You want to cancel your own existing appointment and book an appointment for your child Roc Demo,
born 2018-01-01. Accept the first appropriate option the receptionist offers, but never confirm an
option before hearing it. Supply information when asked, correct misunderstandings, and keep each
turn natural and short. Do not invent backend IDs, receipts, or extra patients. Say you are done
only when both requests are addressed, or if the receptionist clearly cannot continue.
Return JSON with text (your next spoken turn) and done (whether you intend to end the call).
"""


class CallerTurn(Model):
    text: str = Field(min_length=1, max_length=500)
    done: bool = False


class ModelCaller:
    def __init__(self, config: Config, store: RunStore, run_id: str, client: httpx.AsyncClient | None = None):
        self.config, self.store, self.run_id = config, store, run_id
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5))
        self.history: list[dict] = []

    async def reply(self, agent_text: str, language: str) -> CallerTurn:
        if not self.config.allow_paid or not self.config.gateway_key:
            raise PermissionError("paid caller simulation is disabled")
        self.history.append({"role": "user", "content": agent_text[:1600]})
        reservation = self.store.reserve(self.run_id, "simulated_caller", 100_000)
        response = await self.client.post("https://ai-gateway.vercel.sh/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.config.gateway_key}"},
            json={"model": self.config.llm_model, "temperature": 0, "max_tokens": 400,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": GOAL + f"\nSpeak only {language}."}, *self.history[-12:]]})
        if response.status_code != 200:
            raise RuntimeError(f"caller model HTTP {response.status_code}")
        body = response.json()
        result = CallerTurn.model_validate_json(body["choices"][0]["message"]["content"])
        self.history.append({"role": "assistant", "content": result.text})
        self.store.event(self.run_id, "caller_usage", {"reservation": reservation, "usage": body.get("usage", {})})
        return result

    async def close(self):
        await self.client.aclose()


async def run_duel(config: Config, store: RunStore, language: str = "en", max_turns: int = 8) -> dict:
    if not config.allow_paid or not config.gateway_key or not 1 <= max_turns <= 8:
        raise PermissionError("agent duels require paid opt-in, a gateway key, and at most eight turns")
    if store.db.execute("PRAGMA database_list").fetchone()[2] == "":
        raise ValueError("paid duels require a persistent budget ledger")
    state = CallState(call_id="duel-" + uuid4().hex, language=language,
                      reference_time=datetime.fromisoformat("2026-09-19T09:00:00+02:00"))
    interpreter = VercelInterpreter(config, store)
    controller = CallController(state, FixtureClinic(), store, Dispatcher(store, "simulation"), interpreter,
                                {**config.manifest(), "execution": "text_agent_duel", "synthetic": True,
                                 "scenario": "cancel-parent-book-child-v1", "max_turns": max_turns})
    caller = ModelCaller(config, store, state.run_id)
    response = TEXT[language]["hello"]
    try:
        for _ in range(max_turns):
            incoming = await caller.reply(response, language)
            reply = await controller.turn(incoming.text)
            if reply is None:
                break
            controller.presented(reply)
            response = reply.text
            if reply.completion or incoming.done:
                break
    except Exception as exc:
        store.event(state.run_id, "error", {"where": "simulator", "error_type": type(exc).__name__})
    finally:
        await caller.close()
        await interpreter.close()
    report = store.report(state.run_id)
    grade = fixture_grade(report, [("cancel", "fixture-appointment-fixture-adult"), ("book", "fixture-child")])
    store.event(state.run_id, "fixture_grade", grade)
    return {**store.report(state.run_id), "metrics": metrics(report), "fixture_grade": grade}
