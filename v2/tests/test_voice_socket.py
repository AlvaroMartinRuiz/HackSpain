from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipecat.frames.frames import TranscriptionFrame
from pipecat.services.stt_service import STTService
from starlette.websockets import WebSocketState

from v2.clinic import Dispatcher, FixtureClinic
from v2.models import CallState
from v2.store import RunStore
from v2.tests.test_voice import FixtureSpeaker
from v2.tests.test_workflow import NOW, booking, decision
from v2.voice import run_voice
from v2.workflow import CallController, TEXT


class SyntheticSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.sent_at = []
        self.client_state = self.application_state = WebSocketState.CONNECTED
        self.headers = {}
        self.closed = False

    async def receive(self):
        return await self.incoming.get()

    async def send_text(self, text):
        if self.closed:
            raise ConnectionError("synthetic socket closed")
        self.sent.append(json.loads(text))
        self.sent_at.append(asyncio.get_running_loop().time())

    async def close(self, **_kwargs):
        self.closed = True
        self.application_state = WebSocketState.DISCONNECTED

    async def audio(self):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps({
            "event": "media", "streamSid": "synthetic-stream", "media": {
                "track": "inbound", "payload": base64.b64encode(b"\x80" * 1600).decode(),
            },
        })})


class SyntheticSTT(STTService):
    def __init__(self, utterances):
        super().__init__(sample_rate=16000, ttfs_p99_latency=0.01)
        self.utterances = iter(utterances)
        self.received = []

    async def run_stt(self, audio):
        self.received.append(audio)
        text = next(self.utterances, None)
        if text:
            yield TranscriptionFrame(text=text, user_id="synthetic-caller", timestamp="2026-09-19T09:00:00Z", finalized=True)


async def eventually(predicate, seconds=5):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.005)


class SocketPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_socket_pipeline_completes_multi_intent_call(self):
        with tempfile.TemporaryDirectory() as root:
            store = RunStore()
            self.addCleanup(store.close)
            utterances = ["Please book for Lina Demo, born 1990-01-01", "Yes",
                          "Also book for Roc Demo, born 2018-01-01", "Yes", "Thank you"]
            choices = iter([booking(), decision({"op": "confirm", "intent_id": "mine", "option": 1,
                                                 "offer_revision": 1, "evidence": "Yes"}),
                            booking("child", "Roc Demo", "2018-01-01"),
                            decision({"op": "confirm", "intent_id": "child", "option": 1,
                                      "offer_revision": 1, "evidence": "Yes"}), decision({"op": "finish"})])
            async def interpret(*_args):
                return next(choices)
            state = CallState(call_id="original-synthetic-call", reference_time=NOW)
            controller = CallController(state, FixtureClinic(), store, Dispatcher(store, "simulation"),
                                        interpreter=SimpleNamespace(decide=interpret))
            config = SimpleNamespace(data_dir=Path(root), call_limit_s=20, completion_grace_s=0.04,
                                     user_speech_timeout_s=0.02, voice_playout_tail_s=0)
            socket = SyntheticSocket()
            stt = SyntheticSTT(utterances)
            voices = {lang: FixtureSpeaker() for lang in ("en", "es", "ca")}
            with patch("v2.voice.voice_services", return_value=(stt, voices)), patch("v2.voice.SileroVADAnalyzer", return_value=None):
                task = asyncio.create_task(run_voice(socket, "synthetic-stream", controller, config))
                try:
                    for index in range(len(utterances)):
                        await eventually(lambda: len([e for e in store.report(state.run_id)["events"]
                                                       if e["kind"] == "voice_response_sent"]) >= index + 1)
                        await socket.audio()
                    await asyncio.wait_for(task, 8)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(state.all_resolved)
            self.assertEqual(state.turn, 5)
            self.assertEqual(len(stt.received), 5)
            self.assertTrue(all(audio and len(audio) % 2 == 0 for audio in stt.received))
            self.assertTrue(socket.closed)
            for intent in state.intents.values():
                self.assertEqual(len(intent.receipts), 1)
                self.assertEqual(intent.receipts[0]["source"], "simulation")
                self.assertEqual(intent.receipts[0]["payload"]["call_id"], "original-synthetic-call")
            media = [(m, timestamp) for m, timestamp in zip(socket.sent, socket.sent_at) if m["event"] == "media"]
            self.assertTrue(media)
            self.assertTrue(all(len(base64.b64decode(m["media"]["payload"])) == 160 for m, _ in media))
            self.assertTrue(all(b[1] - a[1] >= 0.018 for a, b in zip(media, media[1:])))
            events = store.report(state.run_id)["events"]
            planned = [event["payload"] for event in events if event["kind"] == "response_planned"]
            self.assertEqual(planned[0]["text"], TEXT["en"]["hello"])
            self.assertFalse([e for e in events if e["kind"] == "pipeline_error"])
            self.assertTrue(any(e["kind"] == "completion_close" for e in events))
            self.assertTrue((Path(root) / "audio" / state.run_id / "outbound.wav").exists())

    async def test_malformed_packet_disconnects_and_finalizes_audio(self):
        with tempfile.TemporaryDirectory() as root:
            store = RunStore()
            self.addCleanup(store.close)
            state = CallState(call_id="invalid-socket", reference_time=NOW)
            controller = CallController(state, FixtureClinic(), store, Dispatcher(store, "simulation"))
            socket = SyntheticSocket()
            config = SimpleNamespace(data_dir=Path(root), call_limit_s=3, completion_grace_s=15)
            await socket.incoming.put({"type": "websocket.receive", "text": '{"event":"media","media":{}}'})
            with patch("v2.voice.voice_services", return_value=(SyntheticSTT([]), {lang: FixtureSpeaker() for lang in ("en", "es", "ca")})), patch("v2.voice.SileroVADAnalyzer", return_value=None):
                await asyncio.wait_for(run_voice(socket, "synthetic-stream", controller, config), 5)
            self.assertTrue(socket.closed)
            self.assertTrue(any(e["kind"] == "audio_output" for e in store.report(state.run_id)["events"]))
            self.assertEqual(state.turn, 0)
