from __future__ import annotations

import asyncio
import hashlib
import math
import time
from datetime import datetime
from uuid import uuid4

import httpx
from pydantic import Field, field_validator

from v2.clinic import Dispatcher, FixtureClinic
from v2.config import Config
from v2.evaluation import evaluation_provenance, save_evaluation
from v2.fixtures.outcomes import CLOCK, LANGUAGES, caller_goal, scenario_oracle
from v2.models import CallState, Model
from v2.providers import VercelInterpreter
from v2.store import RunStore
from v2.workflow import CallController, TEXT

GOAL = """You are a synthetic caller, not the receptionist. Supply information when asked, correct
misunderstandings, and keep each turn natural and short. Do not invent backend IDs, receipts or
extra patients. Never confirm an option before hearing it. Speak only the requested language.
Return JSON with text (your next spoken turn) and done (whether you intend to end the call).
"""


class CallerTurn(Model):
    text: str = Field(min_length=1, max_length=500)
    done: bool = Field(default=False, strict=True)

    @field_validator("text")
    @classmethod
    def require_speech(cls, value):
        if not value.strip():
            raise ValueError("caller turn cannot be blank")
        return value


class ModelCaller:
    def __init__(self, config: Config, store: RunStore, run_id: str, client: httpx.AsyncClient | None = None,
                 *, scenario: str = "multi_intent"):
        self.config, self.store, self.run_id = config, store, run_id
        self.goal = caller_goal(scenario)
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5))
        self._owns_client = client is None
        self.history: list[dict] = []
        self.turns = 0

    async def reply(self, agent_text: str, language: str) -> CallerTurn:
        if not self.config.allow_paid or not self.config.gateway_key:
            raise PermissionError("paid caller simulation is disabled")
        if self.store.db.execute("PRAGMA database_list").fetchone()[2] == "":
            raise ValueError("paid caller simulation requires a persistent run store")
        if language not in LANGUAGES or not isinstance(agent_text, str) or not 1 <= len(agent_text) <= 16_000:
            raise ValueError("caller requires a bounded observed reply and supported language")
        if self.turns >= 8:
            raise ValueError("caller turn limit exceeded")
        self.turns += 1
        self.history = [*self.history[-11:], {"role": "user", "content": agent_text[:1600]}]
        reservation = uuid4().hex
        started = time.monotonic()
        phase, status = "request", None
        try:
            response = await self.client.post("https://ai-gateway.vercel.sh/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.config.gateway_key}"},
                json={"model": self.config.llm_model, "temperature": 0, "max_tokens": 400,
                      "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": GOAL + self.goal + f"\nSpeak only {language}."}, *self.history]})
            status = response.status_code
            if status != 200:
                raise RuntimeError("caller provider rejected request")
            phase = "validation"
            if len(response.content) > 65_536:
                raise ValueError("caller provider response exceeds limit")
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") not in {"stop", None}:
                raise ValueError("incomplete caller output")
            result = CallerTurn.model_validate_json(choice["message"]["content"])
            self.history.append({"role": "assistant", "content": result.text})
            usage = body.get("usage") or {}
            usage = {key: value for key, value in usage.items() if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                     and type(value) is int and 0 <= value <= 1_000_000_000} if isinstance(usage, dict) else {}
            self.store.event(self.run_id, "caller_usage", {"reservation": reservation, "usage": usage,
                             "model": self.config.llm_model, "elapsed_ms": round((time.monotonic() - started) * 1000),
                             "actual_cost": None})
            return result
        except (Exception, asyncio.CancelledError) as exc:
            self.store.event(self.run_id, "provider_error", {"provider": "simulated_caller", "error_type": type(exc).__name__,
                             "phase": phase, "http_status": status, "model": self.config.llm_model,
                             "reservation": reservation, "elapsed_ms": round((time.monotonic() - started) * 1000)})
            raise

    async def close(self):
        if self._owns_client:
            await self.client.aclose()


async def run_duel(config: Config, store: RunStore, language: str = "en", max_turns: int = 8,
                   *, max_seconds: float = 120, scenario: str = "multi_intent", split: str = "regression") -> dict:
    if not config.allow_paid or not config.gateway_key:
        raise PermissionError("agent duels require paid opt-in and a gateway key")
    if type(max_turns) is not int or not 1 <= max_turns <= 8:
        raise ValueError("agent duels permit one to eight turns")
    if type(max_seconds) not in (float, int) or not 0 < max_seconds <= 120 or not math.isfinite(max_seconds):
        raise ValueError("agent duel time limit must be greater than zero and at most 120 seconds")
    if language not in LANGUAGES or split not in {"regression", "holdout"}:
        raise ValueError("unsupported language or dataset split")
    goal = caller_goal(scenario)
    if store.db.execute("PRAGMA database_list").fetchone()[2] == "":
        raise ValueError("paid duels require a persistent run store")
    state = CallState(call_id="duel-" + uuid4().hex, language=language,
                      reference_time=datetime.fromisoformat(CLOCK))
    store.claim_experiment(state.run_id, math.ceil(max_seconds) + 30)
    caller = interpreter = controller = None
    cancelled = False
    status, reason, turns = "timed_out", "turn_limit", 0
    started = time.monotonic()
    try:
        interpreter = VercelInterpreter(config, store)
        controller = CallController(state, FixtureClinic(), store, Dispatcher(store, "simulation"), interpreter,
                                    {**evaluation_provenance(config.manifest()), "execution": "text_agent_duel",
                                     "scenario": scenario + "-v1", "dataset_split": split,
                                     "caller_model": config.llm_model, "caller_prompt_sha256": hashlib.sha256((GOAL + goal).encode()).hexdigest(),
                                     "max_turns": max_turns, "max_seconds": max_seconds, "concurrency": 1,
                                     "oracle_visibility": "evaluator_only", "scripted_decisions": False})
        caller = ModelCaller(config, store, state.run_id, scenario=scenario)
        store.event(state.run_id, "session_started", {"status": "active", "transport": "text"})
        response = TEXT[language]["hello"]
        async with asyncio.timeout(max_seconds):
            for _ in range(max_turns):
                incoming = await caller.reply(response, language)
                turns += 1
                reply = await controller.turn(incoming.text)
                if reply is None:
                    status, reason = "disconnected", "response_cancelled"
                    break
                controller.presented(reply)
                response = reply.text
                if store.report(state.run_id)["metrics"]["errors"]:
                    status, reason = "error", "agent_error"
                    break
                if reply.completion:
                    status, reason = "completed", "agent_completed"
                    break
                if incoming.done:
                    status, reason = "disconnected", "caller_ended"
                    break
    except TimeoutError:
        status, reason = "timed_out", "time_limit"
    except asyncio.CancelledError:
        status, reason = "disconnected", "cancelled"
        cancelled = True
    except Exception as exc:
        status, reason = "error", "simulation_error"
        if controller is None:
            raise
        store.event(state.run_id, "error", {"where": "simulator", "error_type": type(exc).__name__})
    finally:
        try:
            for client in (caller, interpreter):
                if client is not None:
                    try:
                        await asyncio.wait_for(client.close(), timeout=5)
                    except Exception as exc:
                        if controller is not None:
                            store.event(state.run_id, "error", {"where": "simulator_cleanup", "error_type": type(exc).__name__})
            if controller is not None:
                store.save(state)
                store.event(state.run_id, "simulation_ended", {"status": status, "reason": reason, "turns": turns,
                            "elapsed_ms": round((time.monotonic() - started) * 1000)})
                store.finish_run(state.run_id, status, reason=reason)
        finally:
            store.release_experiment(state.run_id)
    result = save_evaluation(store, store.report(state.run_id), scenario_oracle(scenario), rubric="six-outcomes-v1")
    if cancelled:
        raise asyncio.CancelledError
    return result
