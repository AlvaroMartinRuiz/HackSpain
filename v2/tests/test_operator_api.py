from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from v2.api import create_app
from v2.config import Config
from v2.evaluation import demo_request
from v2.providers import VercelInterpreter
from v2.platform_api.client import SubmitResult
from v2.models import MAX_CALL_TURNS, Intent, Operation, TurnDecision


class OperatorAPITests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = Config(operator_token="operator-test", allow_paid=True, gateway_key="gateway-test",
                             deepgram_key="speech-test", cartesia_key="voice-test", elevenlabs_key="ca-test",
                             voice_es="spanish-test", mode="simulation", data_dir=Path(self.folder.name))
        self.headers = {"X-V2-Token": "operator-test"}
        self.origin = {"Origin": "http://testserver"}

    def handshake(self, ticket):
        return {"event": "start", "streamSid": ticket["stream_sid"], "start": {
            "callSid": ticket["call_id"], "streamSid": ticket["stream_sid"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        }}

    def ticket(self, client, **fields):
        response = client.post("/api/voice/ticket", headers={**self.headers, **self.origin},
                               json={"language": "es", "mode": "simulation", **fields})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_history_is_authenticated_bounded_and_contains_persisted_runs(self):
        with TestClient(create_app(self.config)) as client:
            self.assertEqual(client.get("/api/runs").status_code, 401)
            run = client.post("/api/rehearse", headers=self.headers, json=demo_request().model_dump()).json()
            response = client.get("/api/runs?limit=1", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["runs"][0]["run_id"], run["run_id"])
            self.assertIsNone(response.json()["runs"][0]["official_grade"])
            self.assertEqual(client.get("/api/runs/invalid!", headers=self.headers).status_code, 404)
            self.assertEqual(client.get("/api/runs?limit=1001", headers=self.headers).status_code, 422)
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_natural_text_session_uses_model_and_never_submits_to_platform(self):
        fixture = demo_request()
        interpreter = AsyncMock(side_effect=[turn.decision for turn in fixture.turns])
        with patch.object(VercelInterpreter, "decide", interpreter), TestClient(create_app(self.config)) as client:
            response = client.post("/api/sessions/text", headers=self.headers, json={"language": "en"})
            self.assertEqual(response.status_code, 200, response.text)
            run_id = response.json()["run_id"]
            self.assertTrue(response.json()["reply"]["text"])
            for turn in fixture.turns:
                response = client.post(f"/api/sessions/{run_id}/turn", headers=self.headers, json={"text": turn.text})
                self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(interpreter.await_count, 4)
            self.assertEqual(response.json()["metrics"]["simulated_receipts"], 2)
            self.assertEqual(response.json()["metrics"]["http_accepted"], 0)
            self.assertEqual(client.delete(f"/api/sessions/{run_id}", headers=self.headers).status_code, 200)
            self.assertEqual(client.post(f"/api/sessions/{run_id}/turn", headers=self.headers,
                                         json={"text": "hello"}).status_code, 404)
            report = client.get(f"/api/runs/{run_id}", headers=self.headers).json()
            self.assertTrue(any(event["kind"] == "session_ended" for event in report["events"]))

    def test_text_and_browser_sessions_cannot_select_live_execution(self):
        with TestClient(create_app(self.config)) as client:
            for path in ("/api/sessions/text", "/api/voice/ticket"):
                self.assertEqual(client.post(path, headers=self.headers,
                                              json={"language": "en", "mode": "live"}).status_code, 422)
                self.assertEqual(client.post(path, headers=self.headers,
                                              json={"language": "en", "gateway_key": "evil"}).status_code, 422)

    def test_approved_carrier_sink_cannot_leak_into_browser_rehearsal(self):
        config = replace(self.config, mode="live", allow_submissions=True, release_approved=True, api_key="clinic-test")
        submitted = AsyncMock(return_value=SubmitResult("no_action", {}, 200, {}, 1))
        async def voice(socket, stream_sid, controller, config):
            intent = Intent(intent_id="request", action="no_action", subject="synthetic caller")
            controller.state.intents[intent.intent_id] = intent
            receipt = await controller.dispatcher.execute(controller.state, intent, "no_action", {"reason": "out_of_scope"})
            await socket.send_json({"event": "receipt", "source": receipt["source"]})
        with patch("v2.platform_api.client.PlatformClient.submit", submitted), patch("v2.voice.run_voice", side_effect=voice):
            with TestClient(create_app(config)) as client:
                with client.websocket_connect("/ws", headers=self.headers) as socket:
                    socket.send_json(self.handshake({"call_id": "carrier-fixture", "stream_sid": "MZfixture"}))
                    self.assertEqual(socket.receive_json()["source"], "platform_receipt")
                submitted.assert_awaited_once_with("no_action", {"call_id": "carrier-fixture", "reason": "out_of_scope"})
                ticket = self.ticket(client)
                with client.websocket_connect("/ws/browser", subprotocols=["v2-voice", "ticket." + ticket["ticket"]],
                                              headers=self.origin) as socket:
                    socket.send_json(self.handshake(ticket))
                    self.assertEqual(socket.receive_json()["event"], "ready")
                    self.assertEqual(socket.receive_json()["source"], "simulation")
                self.assertEqual(submitted.await_count, 1)

    def test_paid_sessions_are_rejected_but_offline_fixture_still_runs(self):
        with TestClient(create_app(replace(self.config, allow_paid=False))) as client:
            for path in ("/api/sessions/text", "/api/voice/ticket"):
                response = client.post(path, headers=self.headers, json={"language": "en"})
                self.assertEqual(response.status_code, 503)
            response = client.post("/api/rehearse", headers=self.headers, json=demo_request().model_dump())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/api/budget", headers=self.headers).json()["committed_microusd"], 0)

    def test_ticket_requires_authentication_and_same_origin(self):
        with TestClient(create_app(self.config)) as client:
            self.assertEqual(client.post("/api/voice/ticket", json={"language": "en"}).status_code, 401)
            response = client.post("/api/voice/ticket", headers={**self.headers, "Origin": "https://attacker.test"},
                                   json={"language": "en"})
            self.assertEqual(response.status_code, 403)
            ticket = self.ticket(client)
            self.assertEqual(ticket["expires_in"], 30)
            self.assertNotIn("operator-test", json.dumps(ticket))
            self.assertNotIn("?", ticket["websocket_path"])

    def test_browser_ticket_is_single_use_and_binds_call_identity(self):
        seen = []
        async def voice(socket, stream_sid, controller, config):
            seen.append((stream_sid, controller.state.call_id, controller.state.mode, controller.state.language))
            await socket.receive_text()
        with patch("v2.voice.run_voice", side_effect=voice), TestClient(create_app(self.config)) as client:
            ticket = self.ticket(client)
            protocols = ["v2-voice", "ticket." + ticket["ticket"]]
            with client.websocket_connect(ticket["websocket_path"], subprotocols=protocols, headers=self.origin) as socket:
                self.assertEqual(socket.accepted_subprotocol, "v2-voice")
                socket.send_json({"event": "connected"})
                socket.send_json(self.handshake(ticket))
                ready = socket.receive_json()
                self.assertEqual(ready["event"], "ready")
                socket.send_json({"event": "stop"})
            self.assertEqual(seen, [(ticket["stream_sid"], ticket["call_id"], "simulation", "es")])
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect(ticket["websocket_path"], subprotocols=protocols, headers=self.origin):
                    pass
            response = client.get("/api/runs/" + ready["run_id"], headers=self.headers)
            self.assertEqual(response.status_code, 200)

    def test_expired_ticket_and_foreign_websocket_origin_are_rejected(self):
        with TestClient(create_app(self.config)) as client:
            ticket = self.ticket(client)
            protocols = ["v2-voice", "ticket." + ticket["ticket"]]
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/ws/browser", subprotocols=protocols,
                                              headers={"Origin": "https://attacker.test"}):
                    pass
            with patch("v2.api.time.monotonic", return_value=time.monotonic() + 31):
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/ws/browser", subprotocols=protocols, headers=self.origin):
                        pass

    def test_browser_cannot_forge_call_sid_and_query_tokens_are_rejected(self):
        with TestClient(create_app(self.config)) as client:
            ticket = self.ticket(client)
            protocols = ["v2-voice", "ticket." + ticket["ticket"]]
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/ws/browser", subprotocols=protocols, headers=self.origin) as socket:
                    handshake = self.handshake(ticket)
                    handshake["start"]["callSid"] = "another-real-call"
                    socket.send_json(handshake)
                    socket.receive_json()
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/ws?token=operator-test"):
                    pass

    def test_completed_voice_status_survives_controller_cleanup(self):
        async def voice(socket, stream_sid, controller, config):
            controller.state.intents["done"] = Intent(intent_id="done", action="cancel", subject="Fixture",
                                                       status="completed")
            controller.store.event(controller.state.run_id, "completion_close", {"epoch": controller.epoch})
            controller.state.completion_requested = False
        with patch("v2.voice.run_voice", side_effect=voice), TestClient(create_app(self.config)) as client:
            ticket = self.ticket(client)
            with client.websocket_connect("/ws/browser", subprotocols=["v2-voice", "ticket." + ticket["ticket"]],
                                          headers=self.origin) as socket:
                socket.send_json(self.handshake(ticket))
                ready = socket.receive_json()
                with self.assertRaises(WebSocketDisconnect):
                    socket.receive_json()
            report = client.get("/api/runs/" + ready["run_id"], headers=self.headers).json()
            ended = [event for event in report["events"] if event["kind"] == "call_ended"]
            self.assertEqual(ended[-1]["payload"]["status"], "completed")

    def test_http_bodies_are_bounded_before_json_validation(self):
        with TestClient(create_app(self.config)) as client:
            response = client.post("/api/rehearse", headers={**self.headers, "Content-Type": "application/json"},
                                   content=b" " * 70000)
            self.assertEqual(response.status_code, 413)
            self.assertEqual(client.get("/health").headers["x-content-type-options"], "nosniff")

    def test_text_collection_uses_the_controller_turn_limit(self):
        result = TurnDecision(language="en", operations=[Operation(op="ask", question="identity")])
        with patch.object(VercelInterpreter, "decide", AsyncMock(return_value=result)), TestClient(create_app(self.config)) as client:
            run_id = client.post("/api/sessions/text", headers=self.headers, json={"language": "en"}).json()["run_id"]
            session = client.app.state.text_sessions[run_id]
            session.controller.state.turn = 20
            response = client.post(f"/api/sessions/{run_id}/turn", headers=self.headers, json={"text": "Hello"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["state"]["turn"], 21)
            session.controller.state.turn = MAX_CALL_TURNS
            self.assertEqual(client.post(f"/api/sessions/{run_id}/turn", headers=self.headers,
                                         json={"text": "Hello"}).status_code, 409)

    def test_busy_text_session_is_not_cancelled_or_reentered(self):
        with TestClient(create_app(self.config)) as client:
            response = client.post("/api/sessions/text", headers=self.headers, json={"language": "en"})
            run_id = response.json()["run_id"]
            session = client.app.state.text_sessions[run_id]
            session.busy = True
            self.assertEqual(client.delete(f"/api/sessions/{run_id}", headers=self.headers).status_code, 409)
            self.assertEqual(client.post(f"/api/sessions/{run_id}/turn", headers=self.headers,
                                         json={"text": "hello"}).status_code, 409)
            session.busy = False


if __name__ == "__main__":
    unittest.main()
