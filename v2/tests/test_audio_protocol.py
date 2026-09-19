from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pipecat.frames.frames import CancelFrame, EndFrame, InputAudioRawFrame, InterruptionFrame

from v2.audio import MediaProtocolError, RecordedSocket, RunTape, parse_message
from v2.voice import ProsperSerializer


def media(payload=b"\xff" * 160, **fields):
    return json.dumps({"event": "media", "streamSid": "stream", "media": {
        "payload": base64.b64encode(payload).decode(), **fields,
    }})


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_invalid_packets_before_codec(self):
        serializer = ProsperSerializer("stream", "original-call")
        await serializer.setup(SimpleNamespace(audio_in_sample_rate=16000))
        invalid = ["null", "[]", "{", "{}", '{"event":1}', media(b""), media(b"a" * 8001),
                   media(track="outbound"), media(timestamp="-1"), media(timestamp=True),
                   media().replace('"stream"', '"other"'),
                   '{"event":"media","streamSid":"stream","media":{"payload":"!!!!"}}',
                   '{"event":"media","streamSid":"stream","media":[]}',
                   '{"event":"start","streamSid":"stream"}',
                   '{"event":"connected"}', " " * 16385]
        for packet in invalid:
            with self.subTest(packet=packet[:100]):
                with self.assertRaises(MediaProtocolError):
                    await serializer.deserialize(packet)

    async def test_resamples_inbound_and_preserves_original_identifiers(self):
        serializer = ProsperSerializer("stream", "original-call")
        await serializer.setup(SimpleNamespace(audio_in_sample_rate=16000))
        frame = await serializer.deserialize(media(b"\xff" * 1600))
        self.assertIsInstance(frame, InputAudioRawFrame)
        self.assertEqual(frame.sample_rate, 16000)
        self.assertEqual(frame.num_channels, 1)
        self.assertGreater(len(frame.audio), 0)
        self.assertEqual(serializer._call_sid, "original-call")
        self.assertEqual(json.loads(await serializer.serialize(InterruptionFrame())),
                         {"event": "clear", "streamSid": "stream"})
        serializer._hang_up_call = AsyncMock(side_effect=AssertionError("REST must not be used"))
        self.assertIsNone(await serializer.serialize(EndFrame()))
        self.assertIsNone(await serializer.serialize(CancelFrame()))
        serializer._hang_up_call.assert_not_called()
        self.assertIsInstance(await serializer.deserialize('{"event":"stop","streamSid":"stream"}'), EndFrame)

    async def test_recording_and_codec_share_validation(self):
        with tempfile.TemporaryDirectory() as root:
            tape = RunTape(Path(root), "recording")
            socket = SimpleNamespace(receive=AsyncMock(return_value={"type": "websocket.receive", "bytes": media().encode()}))
            wrapped = RecordedSocket(socket, tape, stream_sid="stream")
            await wrapped.receive()
            self.assertEqual(tape.frames["inbound"], 1)
            socket.receive.return_value = {"type": "websocket.receive", "text": media(timestamp="-8")}
            with self.assertRaises(MediaProtocolError):
                await wrapped.receive()
            self.assertEqual(tape.frames["inbound"], 1)

    async def test_send_timeout_does_not_count_as_audio(self):
        async def blocked(_):
            await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as root:
            tape = RunTape(Path(root), "timeout")
            wrapped = RecordedSocket(SimpleNamespace(send_text=blocked), tape, send_timeout_s=0.01)
            with self.assertRaises(TimeoutError):
                await wrapped.send_text(media(b"\x80" * 160))
            self.assertEqual(tape.signal_frames, 0)

    async def test_stale_audio_waiting_for_socket_lock_is_not_sent(self):
        with tempfile.TemporaryDirectory() as root:
            tape = RunTape(Path(root), "lock-race")
            socket = SimpleNamespace(send_text=AsyncMock())
            wrapped = RecordedSocket(socket, tape)
            current = True
            await wrapped._send_lock.acquire()
            task = asyncio.create_task(wrapped.send_text_if_current(media(b"\x80" * 160), lambda: current))
            await asyncio.sleep(0)
            current = False
            wrapped._send_lock.release()
            self.assertFalse(await task)
            socket.send_text.assert_not_called()
            self.assertEqual(tape.signal_frames, 0)

    async def test_realtime_input_budget_prevents_unbounded_audio_queue(self):
        with tempfile.TemporaryDirectory() as root:
            tape = RunTape(Path(root), "burst")
            socket = SimpleNamespace(receive=AsyncMock(return_value={"type": "websocket.receive", "text": media(b"\xff" * 8000)}))
            wrapped = RecordedSocket(socket, tape)
            await wrapped.receive()
            await wrapped.receive()
            with self.assertRaises(MediaProtocolError):
                await wrapped.receive()
            self.assertEqual(tape.frames["inbound"], 2)
            self.assertEqual(tape.protocol_errors, 1)

    def test_mark_names_are_bounded_and_not_arbitrary_objects(self):
        for name in (None, {}, "x" * 129):
            with self.assertRaises(MediaProtocolError):
                parse_message(json.dumps({"event": "mark", "streamSid": "stream", "mark": {"name": name}}), "stream")
        value, payload = parse_message('{"event":"mark","streamSid":"stream","mark":{"name":"v2-1"}}', "stream")
        self.assertEqual(value["mark"]["name"], "v2-1")
        self.assertIsNone(payload)

    def test_tape_cap_and_digital_silence(self):
        with tempfile.TemporaryDirectory() as root:
            tape = RunTape(Path(root), "bounded")
            tape.cap = 320
            tape.append("outbound", b"\x7f" * 160, 0)
            tape.append("inbound", b"\x80" * 160, 10**9)
            self.assertEqual(tape.signal_frames, 0)
            self.assertLessEqual(len(tape.tracks["inbound"]), 320)
            self.assertEqual(tape.save()["audio_status"], "silent")
