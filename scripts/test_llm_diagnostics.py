from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = ":memory:"
os.environ["TTS_WARM_CACHE"] = "false"

import httpx

from src.agent.llm import LLMClient, LLMError
from src.domain.catalog import Catalog
from src.obs.store import CallStore
from src.telephony.session import CallSession


def event(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield event({"content": "Hello. "}).encode()
        raise httpx.ReadTimeout("")


class LLMDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.llm = LLMClient()
        await self.llm.aclose()
        self.addAsyncCleanup(self.llm.aclose)

    def transport(self, handler):
        self.llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                           base_url="https://llm.test")

    async def completion(self):
        return [item async for item in self.llm.stream([], [])]

    async def test_empty_timeout_has_type_phase_and_nonempty_message(self):
        def fail(_request):
            raise httpx.ReadTimeout("")
        self.transport(fail)
        with self.assertRaises(LLMError) as raised:
            await self.completion()
        info = raised.exception.as_dict()
        self.assertEqual(info["error_type"], "ReadTimeout")
        self.assertEqual(info["phase"], "request")
        self.assertTrue(info["detail"])
        self.assertGreaterEqual(info["elapsed_ms"], 0)
        self.assertFalse(info["output_started"])
        self.assertIn("model", info)

    async def test_timeout_after_output_is_distinct(self):
        self.transport(lambda _: httpx.Response(200, stream=BrokenStream()))
        with self.assertRaises(LLMError) as raised:
            await self.completion()
        info = raised.exception.as_dict()
        self.assertEqual(info["error_type"], "ReadTimeout")
        self.assertEqual(info["phase"], "stream")
        self.assertTrue(info["output_started"])
        self.assertIsNotNone(info["first_token_ms"])

    async def test_http_failure_does_not_log_provider_body_or_credentials(self):
        self.transport(lambda _: httpx.Response(429, text="PRIVATE patient and Bearer SECRET"))
        with self.assertRaises(LLMError) as raised:
            await self.completion()
        info = raised.exception.as_dict()
        self.assertEqual(info["http_status"], 429)
        self.assertEqual(info["error_type"], "HTTPStatusError")
        self.assertNotIn("PRIVATE", str(info))
        self.assertNotIn("SECRET", str(info))

    async def test_transport_exception_message_is_not_logged(self):
        def fail(_request):
            raise httpx.ConnectError("Bearer SECRET at private-url")
        self.transport(fail)
        with self.assertRaises(LLMError) as raised:
            await self.completion()
        self.assertEqual(raised.exception.as_dict()["error_type"], "ConnectError")
        self.assertNotIn("SECRET", str(raised.exception.as_dict()))

    async def test_sse_error_is_not_a_successful_empty_completion(self):
        self.transport(lambda _: httpx.Response(200, text='data: {"error":{"message":"PRIVATE"}}\n\n'))
        with self.assertRaises(LLMError) as raised:
            await self.completion()
        self.assertEqual(raised.exception.as_dict()["error_type"], "ProviderStreamError")
        self.assertNotIn("PRIVATE", str(raised.exception.as_dict()))

    async def test_truncated_tool_stream_cannot_execute_partial_arguments(self):
        data = event({"tool_calls": [{"index": 0, "id": "one", "function": {
            "name": "book_slot", "arguments": '{"option":'
        }}]})
        self.transport(lambda _: httpx.Response(200, text=data))
        with self.assertRaises(LLMError) as raised:
            await self.completion()
        self.assertEqual(raised.exception.as_dict()["error_type"], "IncompleteStream")
        self.assertTrue(raised.exception.as_dict()["output_started"])

    async def test_finished_stream_is_valid_without_done_marker(self):
        self.transport(lambda _: httpx.Response(200, text=event({"content": "Hello"}) + event({}, "stop")))
        result = await self.completion()
        self.assertEqual(result[-1][0], "done")
        self.assertEqual(result[-1][1].text, "Hello")

    async def test_empty_stream_is_diagnostic_failure(self):
        self.transport(lambda _: httpx.Response(200, text="data: [DONE]\n\n"))
        with self.assertRaises(LLMError) as raised:
            await self.completion()
        self.assertEqual(raised.exception.as_dict()["error_type"], "EmptyCompletion")

    async def test_agent_records_structured_failure_and_round(self):
        def fail(_request):
            raise httpx.ReadTimeout("")
        self.transport(fail)
        store = CallStore(":memory:")
        self.addCleanup(store._db.close)
        store.open_call("diagnostic", None)
        async def send(_message):
            pass
        session = CallSession("diagnostic", "test", None, send, Catalog.load(), store, self.llm,
                              text_mode=True, dry_run=True)
        self.addAsyncCleanup(session.client.aclose)
        await session.agent.handle("I would like an appointment")
        error = store.get("diagnostic").errors[0]
        self.assertEqual(error["error_type"], "ReadTimeout")
        self.assertEqual(error["round"], 1)
        self.assertEqual(error["where"], "llm")
        self.assertTrue(error["detail"])


if __name__ == "__main__":
    unittest.main()
