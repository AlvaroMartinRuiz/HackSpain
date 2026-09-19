from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = ":memory:"
os.environ["TTS_WARM_CACHE"] = "false"

from src.domain.catalog import Catalog
from src.obs.store import CallStore
from src.telephony.session import CallSession


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = CallStore(":memory:")
        self.addCleanup(self.store._db.close)

    async def record_call(self, call_id, *, frames=0, signal=0, submit=False,
                          text=False, complete=True, close=True):
        call = self.store.open_call(call_id, None)
        await self.store.record(call_id, "audio_output", {
            "text_mode": text, "frames": frames, "bytes": frames * 160,
            "non_silent_frames": signal, "complete": complete,
        })
        if submit:
            await self.store.record(call_id, "submit", {"action": "book", "accepted": True, "status": 200})
        if close:
            self.store.close_call(call_id)
        return call

    async def test_submission_and_silence_are_independent(self):
        await self.record_call("submitted-silent", submit=True)
        await self.record_call("spoken-no-record", frames=1, signal=1)
        stats = self.store.aggregate()
        self.assertEqual(stats["missing_submission_calls"], 1)
        self.assertEqual(stats["silent_calls"], 1)
        self.assertEqual(stats["with_accepted_submission"], 1)
        self.assertEqual(self.store.get("spoken-no-record").summary()["audio_status"], "signal")
        self.assertEqual(self.store.get("submitted-silent").summary()["audio_status"], "silent")

    async def test_silence_only_frames_are_not_signal(self):
        call = await self.record_call("digital-silence", frames=50)
        self.assertEqual(call.summary()["audio_status"], "silent")
        self.assertEqual(self.store.aggregate()["silent_calls"], 1)

    async def test_live_and_rehearsal_calls_are_not_silent_failures(self):
        await self.record_call("live", close=False, complete=False)
        text = await self.record_call("text", text=True, submit=True)
        stats = self.store.aggregate()
        self.assertEqual(stats["silent_calls"], 0)
        self.assertEqual(stats["missing_submission_calls"], 0)
        self.assertEqual(text.summary()["audio_status"], "not_applicable")

    async def test_legacy_and_incomplete_measurements_are_unknown(self):
        self.store.open_call("legacy", None)
        await self.store.record("legacy", "agent_said", {"text": "Hello"})
        await self.store.record("legacy", "tts", {"bytes": 1600})
        self.store.close_call("legacy")
        await self.record_call("incomplete", complete=False)
        self.assertEqual(self.store.aggregate()["silent_calls"], 0)
        self.assertEqual(self.store.aggregate()["audio_unknown_calls"], 2)
        self.assertEqual(self.store.get("legacy").summary()["audio_status"], "unknown")

    async def test_legacy_rehearsal_event_excludes_audio(self):
        self.store.open_call("old-rehearsal", None)
        await self.store.record("old-rehearsal", "call_started", {"rehearsal": True})
        self.store.close_call("old-rehearsal")
        self.assertEqual(self.store.get("old-rehearsal").summary()["audio_status"], "not_applicable")
        self.assertEqual(self.store.aggregate()["audio_unknown_calls"], 0)

    async def test_snapshots_do_not_double_count_and_history_replays(self):
        call = await self.record_call("replay", frames=1, signal=1, complete=False, close=False)
        for _ in range(2):
            await self.store.record("replay", "audio_output", {
                "text_mode": False, "frames": 20, "bytes": 3200,
                "non_silent_frames": 10, "complete": True,
            })
        self.store.close_call("replay")
        self.assertEqual(call.metrics["outbound_audio"]["frames"], 20)
        self.assertEqual(self.store.history()[0]["audio_status"], "signal")
        self.store._finished.clear()
        restored = self.store.load("replay")
        self.assertEqual(restored.summary()["audio_status"], "signal")
        self.assertEqual(restored.metrics["outbound_audio"]["non_silent_frames"], 10)

    async def test_dry_run_is_not_counted_as_an_http_receipt(self):
        self.store.open_call("rehearsal", None)
        await self.store.record("rehearsal", "submit", {
            "action": "book", "accepted": True, "status": 200, "dry_run": True,
        })
        self.store.close_call("rehearsal")
        self.assertEqual(self.store.aggregate()["with_accepted_submission"], 0)
        self.assertTrue(self.store.get("rehearsal").summary()["actions"][0]["dry_run"])

    async def test_rejected_attempt_is_not_a_missing_submission(self):
        await self.record_call("rejected", frames=1, signal=1, close=False)
        await self.store.record("rejected", "submit", {"action": "book", "accepted": False, "status": 422})
        self.store.close_call("rejected")
        stats = self.store.aggregate()
        self.assertEqual(stats["missing_submission_calls"], 0)
        self.assertEqual(stats["with_accepted_submission"], 0)
        self.assertEqual(stats["silent_calls"], 0)


class SessionAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = CallStore(":memory:")
        self.addCleanup(self.store._db.close)
        self.store.open_call("audio-test", None)
        self.send = AsyncMock()
        self.session = CallSession("audio-test", "test-stream", None, self.send, Catalog.load(),
                                   self.store, None, text_mode=True, dry_run=True)
        self.session.text_mode = False
        self.addAsyncCleanup(self.session.client.aclose)

    async def test_successful_frames_and_both_mulaw_silence_codes(self):
        await self.session._send_media(b"\xff" * 160)
        await self.session._send_media(b"\x7f" * 160)
        await self.session._send_media(b"\x80" * 160)
        await self.session._send_media(b"\x80" * 160)
        await self.session._record_audio_output(complete=True)
        audio = self.store.get("audio-test").metrics["outbound_audio"]
        self.assertEqual(audio["frames"], 4)
        self.assertEqual(audio["bytes"], 640)
        self.assertEqual(audio["non_silent_frames"], 2)
        events = [e for e in self.store.replay("audio-test") if e["kind"] == "audio_output"]
        self.assertEqual(len(events), 3)

    async def test_failed_socket_send_does_not_count(self):
        self.send.side_effect = ConnectionError("closed")
        with self.assertRaises(ConnectionError):
            await self.session._send_media(b"\x80" * 160)
        await self.session._record_audio_output(complete=True)
        audio = self.store.get("audio-test").metrics["outbound_audio"]
        self.assertEqual(audio["frames"], 0)
        self.assertEqual(audio["non_silent_frames"], 0)

    async def test_finalize_without_stop_records_zero_audio(self):
        with patch.object(self.session, "_save_tape"), patch.object(
            self.session, "_guarantee_submission", new_callable=AsyncMock
        ):
            await self.session.finalize()
        self.assertEqual(self.store.aggregate()["silent_calls"], 1)
        self.assertEqual(self.store.aggregate()["missing_submission_calls"], 1)

    async def test_audio_is_reported_before_submission_failure(self):
        with patch.object(self.session, "_save_tape"), patch.object(
            self.session, "_guarantee_submission", new_callable=AsyncMock,
            side_effect=RuntimeError("submission failed")
        ), self.assertRaises(RuntimeError):
            await self.session.finalize()
        self.assertTrue(self.store.get("audio-test").metrics["outbound_audio"]["complete"])

    async def test_synthesis_does_not_count_as_socket_output(self):
        await self.session.record("agent_said", {"text": "Hello"})
        await self.session.record("tts", {"bytes": 1600})
        await self.session._record_audio_output(complete=True)
        self.store.close_call("audio-test")
        self.assertEqual(self.store.aggregate()["silent_calls"], 1)


if __name__ == "__main__":
    unittest.main()
