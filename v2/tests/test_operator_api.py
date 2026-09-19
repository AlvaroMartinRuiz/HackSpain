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
                             voice_es="spanish-test", mode="simulation", data_dir=Path(self.folder.name), public_browser_calls=False,
                             public_operator_tools=False, public_carrier_calls=False)
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

    def test_console_allows_same_origin_microphone_without_camera_or_embedding(self):
        with TestClient(create_app(self.config)) as client:
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["permissions-policy"], "microphone=(self), camera=()")
            self.assertEqual(response.headers["x-frame-options"], "DENY")
            self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

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

    def test_public_operator_tools_expose_demo_records_without_credentials(self):
        config = replace(self.config, public_operator_tools=True, operator_token="")
        with TestClient(create_app(config)) as client:
            self.assertEqual(client.get("/api/budget").status_code, 200)
            response = client.post("/api/rehearse", headers=self.origin, json=demo_request().model_dump())
            self.assertEqual(response.status_code, 200)
            run_id = response.json()["run_id"]
            folder = config.data_dir / "audio" / run_id
            folder.mkdir(parents=True)
            (folder / "inbound.wav").write_bytes(b"RIFF-fixture-audio")
            for path in ("/health", "/api/budget", "/api/runs", f"/api/runs/{run_id}", f"/api/runs/{run_id}/audio/inbound"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                for secret in (config.gateway_key, config.deepgram_key, config.cartesia_key, config.elevenlabs_key):
                    self.assertNotIn(secret, response.text)
            self.assertTrue(client.get("/health").json()["public_operator_tools"])
            self.assertFalse(client.get("/health").json()["public_carrier_calls"])
            self.assertNotIn("V2_OPERATOR_TOKEN", config.missing_text())
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/ws"):
                    pass

    def test_public_operator_writes_require_origin_and_preserve_paid_and_mode_gates(self):
        config = replace(self.config, public_operator_tools=True)
        with TestClient(create_app(config)) as client:
            for headers in ({}, {"Origin": "https://attacker.test"}):
                self.assertEqual(client.post("/api/rehearse", headers=headers, json=demo_request().model_dump()).status_code, 403)
            self.assertEqual(client.post("/api/sessions/text", headers=self.origin, json={"mode": "live"}).status_code, 422)
            self.assertEqual(client.post("/api/rehearse", headers=self.headers, json=demo_request().model_dump()).status_code, 200)
            client.app.state.store.reserve("demo-budget", "test", 29_950_000)
            self.assertEqual(client.post("/api/sessions/text", headers=self.origin, json={}).status_code, 402)
        with TestClient(create_app(replace(config, allow_paid=False))) as client:
            self.assertEqual(client.post("/api/sessions/text", headers=self.origin, json={}).status_code, 503)

    def test_public_operator_requests_are_rate_limited_and_voice_keeps_public_limits(self):
        config = replace(self.config, public_operator_tools=True)
        with TestClient(create_app(config)) as client:
            response = client.post("/api/voice/ticket", headers=self.origin, json={})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(all(ticket.public_peer for ticket in client.app.state.voice_tickets.values()))
            peer = next(iter(client.app.state.operator_request_times))
            client.app.state.operator_request_times[peer] = [time.monotonic()] * 60
            self.assertEqual(client.post("/api/rehearse", headers=self.origin, json=demo_request().model_dump()).status_code, 429)
            self.assertEqual(client.get("/api/budget").status_code, 200)

    def test_public_carrier_accepts_headerless_calls_without_unlocking_tools_or_actions(self):
        config = replace(self.config, public_carrier_calls=True, operator_token="")
        seen = []
        async def voice(socket, stream_sid, controller, config, on_ready=None, **_):
            if on_ready:
                await on_ready()
            seen.append((controller.state.mode, controller.state.call_id))
            await socket.send_json({"event": "probe", "mode": controller.state.mode})
        with patch("v2.voice.run_voice", side_effect=voice), TestClient(create_app(config)) as client:
            self.assertNotIn("V2_OPERATOR_TOKEN", config.missing_voice())
            with client.websocket_connect("/ws") as socket:
                socket.send_json(self.handshake({"call_id": "prosper-headerless", "stream_sid": "MZpublic"}))
                self.assertEqual(socket.receive_json(), {"event": "probe", "mode": "simulation"})
            self.assertEqual(seen, [("simulation", "prosper-headerless")])
            self.assertEqual(client.get("/api/budget").status_code, 401)
            self.assertTrue(client.get("/health").json()["public_carrier_calls"])
            self.assertFalse(client.get("/health").json()["live_cutover_enabled"])
        with self.assertRaises(ValueError):
            create_app(replace(config, mode="live", allow_submissions=True, release_approved=False))

    def test_public_carrier_keeps_query_origin_rate_and_budget_guards(self):
        config = replace(self.config, public_carrier_calls=True)
        with TestClient(create_app(config)) as client:
            for path, headers in (("/ws?token=bad", {}), ("/ws", {"Origin": "https://attacker.test"})):
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect(path, headers=headers):
                        pass
            with client.websocket_connect("/ws"):
                pass
            peer = next(iter(client.app.state.carrier_connect_times))
            client.app.state.carrier_connect_times[peer] = [time.monotonic()] * 60
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/ws"):
                    pass
            with client.websocket_connect("/ws", headers=self.headers):
                pass
            client.app.state.carrier_connect_times.clear()
            client.app.state.store.reserve("carrier-budget", "test", 29_000_000)
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/ws"):
                    pass
        with TestClient(create_app(replace(config, allow_paid=False))) as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/ws"):
                    pass

    def test_public_calling_requires_explicit_enablement_and_same_origin(self):
        with TestClient(create_app(self.config)) as client:
            self.assertEqual(client.post("/api/public/voice/ticket", headers=self.origin, json={}).status_code, 403)
        config = replace(self.config, public_browser_calls=True, api_key="clinic-test")
        with TestClient(create_app(config)) as client:
            for origin in ({}, {"Origin": "https://attacker.test"}, {"Origin": "null"}):
                self.assertEqual(client.post("/api/public/voice/ticket", headers=origin, json={}).status_code, 403)
            for fields in ({"mode": "live"}, {"language": "en"}, {"operator_token": "operator-test"}):
                self.assertEqual(client.post("/api/public/voice/ticket", headers=self.origin, json=fields).status_code, 422)
            self.assertTrue(client.get("/health").json()["public_voice_ready"])

    def test_public_call_is_single_use_read_only_and_does_not_unlock_private_data(self):
        config = replace(self.config, public_browser_calls=True, api_key="clinic-test", mode="live",
                         allow_submissions=True, release_approved=True)
        submitted = AsyncMock()
        async def voice(socket, stream_sid, controller, config, on_ready=None, **_):
            if on_ready:
                await on_ready()
            self.assertEqual(controller.state.mode, "practice")
            intent = Intent(intent_id="request", action="no_action", subject="synthetic caller")
            controller.state.intents[intent.intent_id] = intent
            receipt = await controller.dispatcher.execute(controller.state, intent, "no_action", {"reason": "out_of_scope"})
            await socket.send_json({"event": "receipt", "source": receipt["source"]})
        with patch("v2.platform_api.client.PlatformClient.submit", submitted), patch("v2.voice.run_voice", side_effect=voice):
            with TestClient(create_app(config)) as client:
                response = client.post("/api/public/voice/ticket", headers=self.origin, json={})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("operator-test", response.text)
                ticket = response.json()
                protocols = ["v2-voice", "ticket." + ticket["ticket"]]
                with client.websocket_connect(ticket["websocket_path"], subprotocols=protocols, headers=self.origin) as socket:
                    socket.send_json(self.handshake(ticket))
                    ready = socket.receive_json()
                    self.assertEqual(ready["event"], "ready")
                    self.assertEqual(socket.receive_json()["source"], "simulation")
                submitted.assert_not_awaited()
                for path in ("/api/budget", "/api/runs", "/api/demo", f"/api/runs/{ready['run_id']}",
                             f"/api/runs/{ready['run_id']}/audio/inbound"):
                    self.assertEqual(client.get(path).status_code, 401)
                for path in ("/api/voice/ticket", "/api/sessions/text"):
                    self.assertEqual(client.post(path, headers=self.origin, json={}).status_code, 401)
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect(ticket["websocket_path"], subprotocols=protocols, headers=self.origin):
                        pass
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/ws"):
                        pass
                self.assertNotIn("operator-test", client.get("/").text)

    def test_public_calling_preserves_provider_and_budget_gates(self):
        config = replace(self.config, public_browser_calls=True, api_key="clinic-test")
        for blocked in (replace(config, allow_paid=False), replace(config, api_key="")):
            with TestClient(create_app(blocked)) as client:
                self.assertFalse(client.get("/health").json()["public_voice_ready"])
                self.assertEqual(client.post("/api/public/voice/ticket", headers=self.origin, json={}).status_code, 503)
        with TestClient(create_app(config)) as client:
            client.app.state.store.reserve("budget-fixture", "test", 29_000_000)
            self.assertEqual(client.post("/api/public/voice/ticket", headers=self.origin, json={}).status_code, 402)
            self.assertEqual(len(client.app.state.voice_tickets), 0)

    def test_public_ticket_rate_limit_ignores_spoofed_forwarded_headers_and_expires(self):
        config = replace(self.config, public_browser_calls=True, api_key="clinic-test")
        with TestClient(create_app(config)) as client:
            for attempt in range(6):
                response = client.post("/api/public/voice/ticket", headers={**self.origin, "X-Forwarded-For": f"192.0.2.{attempt}"}, json={})
                self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(client.app.state.voice_tickets), 1)
            self.assertEqual(client.post("/api/public/voice/ticket", headers=self.origin, json={}).status_code, 429)
            with patch("v2.api.time.monotonic", return_value=time.monotonic() + 61):
                self.assertEqual(client.post("/api/public/voice/ticket", headers=self.origin, json={}).status_code, 200)

    def test_public_calls_have_separate_concurrency_and_bounded_rate_state(self):
        config = replace(self.config, public_browser_calls=True, api_key="clinic-test")
        async def voice(socket, stream_sid, controller, config, on_ready=None, **_):
            if on_ready:
                await on_ready()
            await socket.receive_json()
        with patch("v2.voice.run_voice", side_effect=voice), TestClient(create_app(config)) as client:
            response = client.post("/api/public/voice/ticket", headers=self.origin, json={})
            self.assertEqual(response.status_code, 200)
            ticket = response.json()
            with client.websocket_connect(ticket["websocket_path"], subprotocols=["v2-voice", "ticket." + ticket["ticket"]], headers=self.origin) as socket:
                socket.send_json(self.handshake(ticket))
                self.assertEqual(socket.receive_json()["event"], "ready")
                self.assertEqual(client.post("/api/public/voice/ticket", headers=self.origin, json={}).status_code, 429)
                self.ticket(client)
                socket.send_json({"event": "stop"})
            self.assertEqual(client.post("/api/public/voice/ticket", headers=self.origin, json={}).status_code, 200)
            client.app.state.public_ticket_times = {str(index): [time.monotonic()] for index in range(1024)}
            self.assertEqual(client.post("/api/public/voice/ticket", headers=self.origin, json={}).status_code, 429)

    def test_voice_language_is_automatic_by_default_and_accepts_auto(self):
        with TestClient(create_app(replace(self.config, default_language="es"))) as client:
            for fields in ({}, {"language": "auto"}):
                response = client.post("/api/voice/ticket", headers={**self.headers, **self.origin}, json=fields)
                self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(all(ticket.language == "es" for ticket in client.app.state.voice_tickets.values()))

    def test_auto_language_follows_the_callers_first_turn(self):
        for language, text in (("en", "Hello, I need an appointment please"),
                               ("es", "Hola, necesito una cita por favor"),
                               ("ca", "Bon dia, voldria demanar cita si us plau")):
            with self.subTest(language=language):
                decision = TurnDecision(language=language, operations=[Operation(op="ask", question="identity")])
                config = replace(self.config, default_language="es")
                with patch.object(VercelInterpreter, "decide", AsyncMock(return_value=decision)), TestClient(create_app(config)) as client:
                    response = client.post("/api/sessions/text", headers=self.headers, json={"language": "auto"})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["reply"]["language"], "es")
                    run_id = response.json()["run_id"]
                    response = client.post(f"/api/sessions/{run_id}/turn", headers=self.headers, json={"text": text})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["state"]["language"], language)
                    self.assertEqual(response.json()["reply"]["language"], language)
                    client.delete(f"/api/sessions/{run_id}", headers=self.headers)

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
        async def voice(socket, stream_sid, controller, config, on_ready=None, **_):
            if on_ready:
                await on_ready()
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
        async def voice(socket, stream_sid, controller, config, on_ready=None, **_):
            if on_ready:
                await on_ready()
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

    def test_browser_ready_waits_for_the_voice_pipeline(self):
        async def voice(socket, stream_sid, controller, config, on_ready=None, **_):
            await socket.send_json({"event": "pipeline_started"})
            await on_ready()
            await socket.receive_text()
        with patch("v2.voice.run_voice", side_effect=voice), TestClient(create_app(self.config)) as client:
            ticket = self.ticket(client)
            with client.websocket_connect("/ws/browser", subprotocols=["v2-voice", "ticket." + ticket["ticket"]],
                                          headers=self.origin) as socket:
                socket.send_json(self.handshake(ticket))
                self.assertEqual(socket.receive_json()["event"], "pipeline_started")
                self.assertEqual(socket.receive_json()["event"], "ready")
                socket.send_json({"event": "stop"})

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
        async def voice(socket, stream_sid, controller, config, on_ready=None, **_):
            if on_ready:
                await on_ready()
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
