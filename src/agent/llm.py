"""A streaming chat client for any OpenAI-compatible endpoint.

Text is yielded as it arrives so speech can start before the model has
finished, and tool calls are reassembled from their deltas.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx

from src.config import settings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = ""

    def parsed_arguments(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    elapsed_ms: int = 0
    first_token_ms: Optional[int] = None
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    pass


_RETRY_STATUSES = {429, 503}
_MAX_ATTEMPTS = 4
# Cloudflare's wholesale Gemini cap 429s a tool follow-up 250 ms later.
_MIN_GAP_S = 1.2


def _retry_delay_s(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return min(12.0, max(0.4, float(raw)))
        except ValueError:
            pass
    return min(8.0, 1.0 * (2 ** attempt))


class LLMClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
                **settings.llm_extra_headers,
            },
            timeout=httpx.Timeout(settings.llm_timeout_s, connect=6.0),
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=30),
        )
        self._gate = asyncio.Lock()
        self._next_ok = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    def _body(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]],
        temperature: Optional[float],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "stream": True,
        }
        if settings.llm_reasoning_effort:
            body["reasoning_effort"] = settings.llm_reasoning_effort
        else:
            body["temperature"] = settings.llm_temperature if temperature is None else temperature
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Yield ("text", chunk) as it arrives, then ("done", Completion)."""
        body = self._body(messages, tools, temperature)
        last_error: Optional[LLMError] = None

        async with self._gate:
            for attempt in range(_MAX_ATTEMPTS):
                wait = self._next_ok - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)

                started = time.perf_counter()
                completion = Completion()
                partial: dict[int, ToolCall] = {}
                yielded = False
                try:
                    async with self._client.stream("POST", "/chat/completions", json=body) as response:
                        if response.status_code in _RETRY_STATUSES:
                            detail = (await response.aread()).decode("utf-8", "replace")[:400]
                            last_error = LLMError(f"HTTP {response.status_code}: {detail}")
                            if attempt + 1 < _MAX_ATTEMPTS:
                                await asyncio.sleep(_retry_delay_s(response, attempt))
                                continue
                            raise last_error

                        if response.status_code != 200:
                            detail = (await response.aread()).decode("utf-8", "replace")[:400]
                            raise LLMError(f"HTTP {response.status_code}: {detail}")

                        async for line in response.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except ValueError:
                                continue

                            usage = chunk.get("usage") or {}
                            if usage:
                                completion.tokens_in = usage.get(
                                    "prompt_tokens", completion.tokens_in
                                )
                                completion.tokens_out = usage.get(
                                    "completion_tokens", completion.tokens_out
                                )

                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta") or {}
                                if choice.get("finish_reason"):
                                    completion.finish_reason = choice["finish_reason"]

                                text = delta.get("content")
                                if text:
                                    if completion.first_token_ms is None:
                                        completion.first_token_ms = int(
                                            (time.perf_counter() - started) * 1000
                                        )
                                    completion.text += text
                                    yielded = True
                                    yield "text", text

                                for call in delta.get("tool_calls") or []:
                                    index = call.get("index", 0)
                                    entry = partial.setdefault(
                                        index,
                                        ToolCall(id=call.get("id", f"call_{index}"), name=""),
                                    )
                                    if call.get("id"):
                                        entry.id = call["id"]
                                    function = call.get("function") or {}
                                    if function.get("name"):
                                        entry.name = function["name"]
                                    if function.get("arguments"):
                                        entry.arguments += function["arguments"]
                except LLMError:
                    raise
                except httpx.HTTPError as exc:
                    last_error = LLMError(str(exc))
                    if yielded or attempt + 1 >= _MAX_ATTEMPTS:
                        raise last_error from exc
                    await asyncio.sleep(min(8.0, 1.0 * (2 ** attempt)))
                    continue

                completion.tool_calls = [
                    partial[key] for key in sorted(partial) if partial[key].name
                ]
                completion.elapsed_ms = int((time.perf_counter() - started) * 1000)
                self._next_ok = time.monotonic() + _MIN_GAP_S
                yield "done", completion
                return

        raise last_error or LLMError("llm failed")
