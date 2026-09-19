"""The turn loop: what the caller said in, speech and tool calls out."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, Optional

from src.agent import phrases
from src.agent.llm import Completion, LLMClient, LLMError
from src.agent.prompt import build_system_prompt, chart_briefing
from src.agent.tools import ToolBox
from src.config import settings

if TYPE_CHECKING:  # pragma: no cover
    from src.telephony.session import CallSession

SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…])$|\n+")
ABBREVIATION_END = re.compile(r"\b(?:dr|dra|sr|sra|mr|mrs|ms)\.$", re.IGNORECASE)
SOFT_BREAK = re.compile(r"(?<=[,;:])\s+")
MAX_HISTORY_MESSAGES = 26
SOFT_FLUSH_CHARS = 130
MIN_SPEECH_CHUNK_CHARS = 64


class Agent:
    def __init__(self, session: "CallSession", llm: LLMClient) -> None:
        self.session = session
        self.llm = llm
        self.tools = ToolBox(session)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt(session.catalog, session.from_number)}
        ]
        self._buffer = ""
        self._pending_briefings: list[str] = []
        # Matches the session's starting language, so the first real detection is
        # what adds the "current language" note, not the default.
        self._language = settings.default_language

    # ---- conversation -------------------------------------------------

    async def greet(self) -> None:
        greeting = settings.greeting
        self.messages.append({"role": "assistant", "content": greeting})
        await self.session.say(greeting, first=True)

    async def handle(self, text: str) -> None:
        """One caller turn, start to finish."""
        started = time.perf_counter()
        self.messages.append({"role": "user", "content": text})
        self._trim()

        liveness = _liveness_response(text, self.session.language)
        if liveness:
            self.messages.append({"role": "assistant", "content": liveness})
            await self.session.say(liveness)
            await self.session.note_response_latency(int((time.perf_counter() - started) * 1000))
            return

        spoke = False
        for round_index in range(settings.llm_max_tool_rounds):
            try:
                completion = await self._run_round()
            except LLMError as exc:
                await self.session.record("error", {"where": "llm", "detail": str(exc)})
                await self.session.say(phrases.pick(phrases.RETRY, self.session.language))
                return

            spoke = spoke or bool(completion.text.strip())

            if not completion.wants_tools:
                if completion.text.strip():
                    self.messages.append({"role": "assistant", "content": completion.text})
                break

            self.messages.append({
                "role": "assistant",
                "content": completion.text or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments or "{}"},
                    }
                    for call in completion.tool_calls
                ],
            })

            for call in completion.tool_calls:
                await self._run_tool(call.id, call.name, call.parsed_arguments())

            for briefing in self._pending_briefings:
                self.messages.append({"role": "system", "content": briefing})
            self._pending_briefings.clear()

            if round_index == settings.llm_max_tool_rounds - 1:
                await self.session.record("error", {
                    "where": "tool_loop", "detail": "hit the tool round cap",
                })

        if not spoke:
            await self.session.say(phrases.pick(phrases.HOLD, self.session.language))

        await self.session.note_response_latency(int((time.perf_counter() - started) * 1000))

    async def _run_round(self) -> Completion:
        """Stream one completion, speaking each sentence as it lands."""
        self._buffer = ""
        speech_parts: list[str] = []
        completion: Optional[Completion] = None

        async for kind, value in self.llm.stream(self._sound_history(), self.tools.schemas()):
            if kind == "text":
                self._buffer += value
                for sentence in self._drain():
                    speech_parts.append(sentence)
                    chunk = " ".join(speech_parts)
                    if len(chunk) >= MIN_SPEECH_CHUNK_CHARS or len(speech_parts) >= 2:
                        await self.session.say(chunk)
                        speech_parts.clear()
            else:
                completion = value

        tail = self._buffer.strip()
        if tail:
            speech_parts.append(tail)
        if speech_parts:
            await self.session.say(" ".join(speech_parts))
        self._buffer = ""

        assert completion is not None
        await self.session.record("llm", {
            "elapsed_ms": completion.elapsed_ms,
            "first_token_ms": completion.first_token_ms,
            "tokens_in": completion.tokens_in,
            "tokens_out": completion.tokens_out,
            "tools": [call.name for call in completion.tool_calls],
            "finish_reason": completion.finish_reason,
        })
        return completion

    def _drain(self) -> list[str]:
        """Pull whole sentences out of the buffer so speech starts early."""
        out: list[str] = []
        while True:
            match = next(
                (
                    candidate for candidate in SENTENCE_END.finditer(self._buffer)
                    if not ABBREVIATION_END.search(
                        self._buffer[:candidate.start()].rstrip()
                    )
                ),
                None,
            )
            if match and match.end() > 0:
                sentence = self._buffer[: match.start()].strip()
                self._buffer = self._buffer[match.end() :]
                if sentence:
                    out.append(sentence)
                continue
            if len(self._buffer) >= SOFT_FLUSH_CHARS:
                breaks = list(SOFT_BREAK.finditer(self._buffer))
                if breaks:
                    cut = breaks[-1]
                    head = self._buffer[: cut.start()].strip()
                    self._buffer = self._buffer[cut.end() :]
                    if head:
                        out.append(head)
                    continue
            return out

    async def _run_tool(self, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        started = time.perf_counter()
        try:
            result = await self.tools.dispatch(name, arguments)
        except Exception as exc:  # a broken tool must not take the call with it
            result = {"error": f"{type(exc).__name__}: {exc}"}
            await self.session.record("error", {"where": f"tool:{name}", "detail": str(exc)})

        elapsed = int((time.perf_counter() - started) * 1000)
        await self.session.record("tool_call", {
            "name": name,
            "arguments": arguments,
            "result": _clip(result),
            "elapsed_ms": elapsed,
        })
        self.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": _as_json(result),
        })

    # ---- housekeeping -------------------------------------------------

    async def brief_on_patient(self, patient: dict[str, Any], context: dict[str, Any]) -> None:
        """Queue the chart, to land once the round's tool results are all in.

        Appending it here would wedge a system message between an assistant's
        tool_calls and their results, which strict gateways reject outright.
        """
        self._pending_briefings.append(chart_briefing(patient, context))

    def note_agent_line(self, text: str) -> None:
        """A line said outside the model, so it knows it was said."""
        self.messages.append({"role": "assistant", "content": text})

    def note_language(self, language: str) -> None:
        """Pin the LLM to the language selected for this call only."""
        code = (language or "es").lower()[:2]
        if code == self._language:
            return
        self._language = code
        label = {"es": "Spanish", "ca": "Catalan", "en": "English"}.get(code, code)
        self.messages.append({
            "role": "system",
            "content": (
                f"The caller's current language is {label} ({code}). Continue naturally in "
                "that language until the caller clearly switches. Do not translate or apologise."
            ),
        })

    def note_interruption(self, spoken_so_far: str) -> None:
        """Record only what the caller actually heard before cutting in."""
        for index in range(len(self.messages) - 1, -1, -1):
            message = self.messages[index]
            if message.get("role") != "assistant" or not message.get("content"):
                continue
            if spoken_so_far:
                message["content"] = spoken_so_far
            elif not message.get("tool_calls"):
                # Cut off before a single frame went out, so the caller heard
                # none of it and it is not part of the conversation at all.
                self.messages.pop(index)
            break

    def _sound_history(self) -> list[dict[str, Any]]:
        """Guarantee every tool_calls message is followed by exactly its results.

        One stray message in between costs the rest of the call, so the shape is
        enforced on the way out rather than trusted.
        """
        out: list[dict[str, Any]] = []
        index = 0
        while index < len(self.messages):
            message = self.messages[index]
            calls = message.get("tool_calls") if message.get("role") == "assistant" else None
            if not calls:
                out.append(message)
                index += 1
                continue

            out.append(message)
            wanted = [call["id"] for call in calls]
            results: dict[str, dict[str, Any]] = {}
            strays: list[dict[str, Any]] = []
            index += 1
            while index < len(self.messages) and len(results) < len(wanted):
                following = self.messages[index]
                if following.get("role") == "tool":
                    call_id = following.get("tool_call_id")
                    if call_id in wanted:
                        results[call_id] = following
                    # A tool result for an unknown id is dropped.
                else:
                    strays.append(following)
                index += 1

            for call_id in wanted:
                out.append(results.get(call_id) or {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": '{"error": "tool produced no result"}',
                })
            out.extend(strays)
        return out

    def _trim(self) -> None:
        if len(self.messages) <= MAX_HISTORY_MESSAGES:
            return
        head = [self.messages[0]]
        tail = self.messages[-(MAX_HISTORY_MESSAGES - 1) :]
        # Never start the kept window on an orphaned tool result.
        while tail and tail[0].get("role") == "tool":
            tail.pop(0)
        self.messages = head + tail


def _as_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)[:4000]


def _clip(value: Any, limit: int = 1200) -> Any:
    text = _as_json(value)
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


def _liveness_response(text: str, language: str) -> Optional[str]:
    """Answer line checks immediately instead of spending an LLM round trip."""
    clean = re.sub(r"[^a-zà-ÿ ]+", " ", (text or "").lower())
    clean = " ".join(clean.split())
    checks = {
        "hello", "hi", "hello are you still there", "are you there",
        "are you still there", "can you hear me",
        "hola", "hola está ahí", "esta ahi", "me oye", "me escucha",
        "em sent", "em sents", "hola em sents",
    }
    if clean not in checks:
        return None
    code = (language or "es").lower()[:2]
    if code == "en":
        return "Yes, I'm here and I can hear you. Please go ahead."
    if code == "ca":
        return "Sí, soc aquí i el sento bé. Digui'm, si us plau."
    return "Sí, estoy aquí y le escucho bien. Dígame, por favor."
