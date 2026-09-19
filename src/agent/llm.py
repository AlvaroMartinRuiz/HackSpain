"""A streaming chat client for any OpenAI-compatible endpoint.

Text is yielded as it arrives so speech can start before the model has
finished, and tool calls are reassembled from their deltas.
"""

from __future__ import annotations

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
    def __init__(self, error_type: str, *, phase: str = "request", http_status: Optional[int] = None,
                 elapsed_ms: int = 0, first_token_ms: Optional[int] = None) -> None:
        detail = f"{error_type} during {phase}"
        if http_status is not None:
            detail += f" (HTTP {http_status})"
        super().__init__(detail)
        self.diagnostic = {
            "where": "llm", "detail": detail, "error_type": error_type,
            "phase": phase, "http_status": http_status, "elapsed_ms": elapsed_ms,
            "first_token_ms": first_token_ms, "output_started": first_token_ms is not None,
            "model": settings.llm_model,
        }

    def as_dict(self) -> dict[str, Any]:
        return dict(self.diagnostic)


class LLMClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
                **settings.llm_extra_headers,
            },
            timeout=httpx.Timeout(30.0, connect=6.0),
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=30),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Yield ("text", chunk) as it arrives, then ("done", Completion)."""
        body: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": settings.llm_temperature if temperature is None else temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        completion = Completion()
        started = time.perf_counter()
        partial: dict[int, ToolCall] = {}
        phase = "request"
        status: Optional[int] = None
        finished = False

        def failure(error_type: str) -> LLMError:
            return LLMError(error_type, phase=phase, http_status=status,
                            elapsed_ms=int((time.perf_counter() - started) * 1000),
                            first_token_ms=completion.first_token_ms)

        try:
            async with self._client.stream("POST", "/chat/completions", json=body) as response:
                status = response.status_code
                if status != 200:
                    raise failure("HTTPStatusError")
                phase = "stream"

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        finished = True
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        raise failure("InvalidStreamJSON") from None
                    if not isinstance(chunk, dict):
                        raise failure("InvalidStreamEvent")
                    if chunk.get("error"):
                        raise failure("ProviderStreamError")

                    usage = chunk.get("usage") or {}
                    if usage:
                        completion.tokens_in = usage.get("prompt_tokens", completion.tokens_in)
                        completion.tokens_out = usage.get("completion_tokens", completion.tokens_out)

                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        if choice.get("finish_reason"):
                            completion.finish_reason = choice["finish_reason"]

                        text = delta.get("content")
                        if (text or delta.get("tool_calls")) and completion.first_token_ms is None:
                            completion.first_token_ms = int((time.perf_counter() - started) * 1000)
                        if text:
                            completion.text += text
                            yield "text", text

                        for call in delta.get("tool_calls") or []:
                            index = call.get("index", 0)
                            entry = partial.setdefault(
                                index, ToolCall(id=call.get("id", f"call_{index}"), name="")
                            )
                            if call.get("id"):
                                entry.id = call["id"]
                            function = call.get("function") or {}
                            if function.get("name"):
                                entry.name = function["name"]
                            if function.get("arguments"):
                                entry.arguments += function["arguments"]
        except httpx.HTTPError as exc:
            raise failure(type(exc).__name__) from exc
        except (TypeError, AttributeError, KeyError, ValueError) as exc:
            raise failure("InvalidStreamEvent") from exc

        if not finished and completion.finish_reason is None:
            raise failure("IncompleteStream")
        completion.tool_calls = [partial[key] for key in sorted(partial) if partial[key].name]
        if not completion.text.strip() and not completion.tool_calls:
            raise failure("EmptyCompletion")
        completion.elapsed_ms = int((time.perf_counter() - started) * 1000)
        yield "done", completion
