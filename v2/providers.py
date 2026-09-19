from __future__ import annotations

import json
import time

import httpx

from v2.config import Config
from v2.models import CallState, TurnDecision
from v2.store import RunStore

INTERPRETER_PROMPT = """Interpret the caller's newest turn as JSON matching the supplied schema.
You do not execute actions or write spoken replies. Use only the caller's words and supplied state.
Create a separate intent for EVERY request and patient. Reuse intent IDs for follow-up turns.
When information is missing, use ask with the appropriate question: identity, appointment, registration_identity or registration_contact.
Avoid asking for information already in the recent dialogue or identity_fields. Use prepare to read registration details back before confirmation.
Use create, then identify (full name and a second identifier), then prepare. Registration uses prepare with registration fields.
Evidence must be an exact quote from the newest caller turn. Do not invent names, identifiers, dates or consent.
Use the caller's own time phrase. The backend resolves dates and constructs all action payloads.
Confirm only an explicitly accepted, previously presented offer; copy its current revision and option number.
Never confirm a changed offer in the same turn it is prepared. A symptom or question is not consent.
Never invent a refusal reason: use the blocking_reason in state. For irrelevant requests create no_action and refuse with out_of_scope.
Finish only after every intent is completed. Additional questions or requests keep the call open.
Language is the language actually spoken, not the nationality of names. Return JSON only.
"""


class VercelInterpreter:
    def __init__(self, config: Config, store: RunStore, client: httpx.AsyncClient | None = None):
        self.config, self.store = config, store
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5))

    async def decide(self, text: str, state: CallState) -> TurnDecision:
        if not self.config.allow_paid or not self.config.gateway_key:
            raise PermissionError("paid interpretation is not configured")
        context = {key: {"action": intent.action, "subject": intent.subject, "status": intent.status,
                         "verified": intent.patient is not None, "revision": intent.revision,
                         "identity_fields": intent.identity_inputs,
                         "blocking_reason": intent.blocking_reason,
                         "appointments": [{k: row.get(k) for k in ("appointment_id", "provider_name", "when", "location_name")}
                                          for row in (intent.patient or {}).get("upcoming", [])[:10]],
                         "offers": [{"option": i, "display": offer.display,
                                     "presented": offer.presented_turn is not None}
                                    for i, offer in enumerate(intent.offers, 1)]}
                   for key, intent in state.intents.items()}
        context = {"intents": context, "recent_dialogue": state.history[-8:]}
        data = json.dumps(context, ensure_ascii=False)
        if len(data) > 24000:
            raise ValueError("conversation state exceeds the bounded model context")
        reservation = self.store.reserve(state.run_id, "vercel_llm", 100_000)
        started = time.monotonic()
        try:
            response = await self.client.post(
                "https://ai-gateway.vercel.sh/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.config.gateway_key}"},
                json={"model": self.config.llm_model, "max_tokens": 1800, "temperature": 0,
                      "response_format": {"type": "json_object"},
                      "messages": [
                          {"role": "system", "content": INTERPRETER_PROMPT + "\nSchema: " + json.dumps(TurnDecision.model_json_schema())},
                          {"role": "user", "content": json.dumps({"state": context, "current_language": state.language,
                                                                  "newest_caller_turn": text}, ensure_ascii=False)},
                      ]},
            )
            if response.status_code != 200:
                raise RuntimeError(f"LLM HTTP {response.status_code}")
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") not in {"stop", None}:
                raise ValueError("incomplete structured model output")
            result = TurnDecision.model_validate_json(choice["message"]["content"])
            self.store.event(state.run_id, "llm_usage", {"model": self.config.llm_model,
                                                        "usage": body.get("usage", {}), "reservation": reservation,
                                                        "elapsed_ms": round((time.monotonic() - started) * 1000)})
            return result
        except Exception as exc:
            self.store.event(state.run_id, "provider_error", {"provider": "vercel_llm", "type": type(exc).__name__,
                                                              "reservation": reservation})
            raise

    async def close(self):
        await self.client.aclose()


class JevClient:
    def __init__(self, config: Config, store: RunStore, client: httpx.AsyncClient | None = None):
        self.config, self.store = config, store
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(7, connect=3))

    async def evaluate(self, run_id: str, turns: list[dict]) -> dict:
        if not self.config.allow_paid or not self.config.jev_url or not self.config.jev_token:
            raise PermissionError("the Jev adapter is not configured for paid evaluation")
        if len(turns) > 40 or any(set(t) != {"role", "text"} or t["role"] not in {"caller", "agent"}
                                  or not isinstance(t["text"], str) or len(t["text"]) > 1000 for t in turns):
            raise ValueError("evaluation accepts only bounded, explicitly selected transcript turns")
        payload = {"schema_version": 1, "run_id": run_id, "question_pack": "conversation-v1", "turns": turns}
        if len(json.dumps(payload).encode()) > 24000:
            raise ValueError("evaluation state too large")
        reservation = self.store.reserve(run_id, "jev", 10_000)
        response = await self.client.post(self.config.jev_url, json=payload,
                                           headers={"Authorization": f"Bearer {self.config.jev_token}"})
        if response.status_code != 200:
            raise RuntimeError(f"Jev adapter HTTP {response.status_code}")
        result = response.json()
        if result.get("question_pack") != "conversation-v1" or result.get("run_id") != run_id:
            raise ValueError("evaluation response does not match this run")
        answers = result.get("answers", {})
        if set(answers) != {"unanswered_request", "repeated_question", "premature_success"}:
            raise ValueError("unexpected evaluation questions")
        for answer in answers.values():
            probability = answer.get("probability")
            if answer.get("type") != "boolean" or type(probability) not in (int, float) or not 0 <= probability <= 1:
                raise ValueError("invalid evaluation probability")
        self.store.event(run_id, "model_assessment", {"source": "jev", "question_pack": "conversation-v1",
                                                     "answers": answers, "usage": result.get("usage"),
                                                     "reservation": reservation, "official_grade": None})
        return result

    async def close(self):
        await self.client.aclose()
