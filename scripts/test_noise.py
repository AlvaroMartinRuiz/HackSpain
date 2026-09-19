from __future__ import annotations

import asyncio
import contextlib
import io
import json
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.check_noise import calibrate, load_wav, main, make_trials, run_trial, word_error_rate
from src.voice.audio import rms


def tone(seconds: float = 0.1, amplitude: int = 2000) -> bytes:
    return struct.pack("<" + "h" * int(seconds * 8000), *(
        round(amplitude * math.sin(2 * math.pi * 440 * i / 8000))
        for i in range(int(seconds * 8000))
    ))


class AudioTests(unittest.TestCase):
    def test_reference_levels_and_snr(self):
        speech, noise, mixed, stats = calibrate(tone(), tone(amplitude=500), 5)
        self.assertAlmostEqual(20 * math.log10(rms(speech) / 32768), -20, places=2)
        self.assertAlmostEqual(20 * math.log10(rms(noise) / 32768), -25, places=2)
        self.assertAlmostEqual(stats["effective_snr_db"], 5, places=2)
        self.assertEqual(len(mixed), len(speech))
        self.assertEqual(stats["mix_clipped_samples"], 0)

    def test_peak_cap_reports_actual_snr(self):
        sparse = struct.pack("<800h", 30000, *([0] * 799))
        _, noise, _, stats = calibrate(tone(), sparse, 5)
        self.assertLessEqual(max(abs(x) for x in struct.unpack("<800h", noise)),
                             round(32768 * 10 ** (-6 / 20)))
        self.assertGreater(stats["effective_snr_db"], 5)
        self.assertGreater(stats["noise_limited_samples"], 0)

    def test_invalid_audio_and_snr(self):
        for speech, noise, snr in [(b"", tone(), 5), (tone(), b"", 5),
                                   (b"\0\0" * 100, tone(), 5),
                                   (tone(), b"\0\0" * 100, 5),
                                   (tone(), tone(), float("nan")),
                                   (tone(), tone(), float("inf"))]:
            with self.subTest(snr=snr, lengths=(len(speech), len(noise))):
                with self.assertRaises(ValueError):
                    calibrate(speech, noise, snr)

    def test_noise_repeats_deterministically(self):
        first = calibrate(tone(), tone(0.02), 5)
        self.assertEqual(first, calibrate(tone(), tone(0.02), 5))
        self.assertEqual(len(first[1]), len(first[0]))

    def test_wav_validation_and_resampling(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.wav"
            for channels, width, rate, valid in [(1, 2, 16000, True), (2, 2, 8000, False),
                                                (1, 1, 8000, False)]:
                with wave.open(str(path), "wb") as handle:
                    handle.setnchannels(channels)
                    handle.setsampwidth(width)
                    handle.setframerate(rate)
                    handle.writeframes(tone())
                if valid:
                    self.assertEqual(len(load_wav(path)), len(tone()) // 2)
                else:
                    with self.assertRaises(ValueError):
                        load_wav(path)

    def test_wer_normalization_and_edits(self):
        self.assertEqual(word_error_rate("¡Buenos días, señor!", "buenos días señor"), 0)
        self.assertAlmostEqual(word_error_rate("one two three", "one four"), 2 / 3)
        self.assertEqual(word_error_rate("one", "one two three"), 2)
        self.assertEqual(word_error_rate("one two", ""), 1)
        with self.assertRaises(ValueError):
            word_error_rate("...", "one")

    def test_trials_have_independent_noise_only_control(self):
        trials = make_trials(tone(), {"room": tone(0.02)}, [5], 0.1)
        self.assertEqual([trial["kind"] for trial in trials], ["clean", "noise_only", "mixed"])
        self.assertEqual(len(trials[0]["audio"]), 800)
        self.assertEqual(len(trials[1]["audio"]), 800)
        self.assertEqual(len(trials[2]["audio"]), 800)


class FakeTranscriber:
    finals = ["please change appointment"]
    partials = ["please change"]
    fail = False
    instances = []

    def __init__(self, on_partial, on_final, on_speech_started, on_notice, **kwargs):
        self.on_partial, self.on_final, self.on_notice = on_partial, on_final, on_notice
        self.finished = False
        self.sent = False
        self.instances.append(self)

    async def start(self):
        if self.fail:
            raise RuntimeError("test error must not enter the report")

    async def push(self, _chunk):
        if not self.sent:
            self.sent = True
            for text in self.partials:
                await self.on_partial(text)

    async def finish(self):
        self.finished = True
        for text in self.finals:
            await self.on_final(text, "en")


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def trial(self, factory=FakeTranscriber):
        return await run_trial(b"\xff" * 160, "Your options are available tomorrow",
                               factory=factory, tail_s=0, timeout_s=2)

    async def test_gate_and_final_drain_without_clinic_or_model(self):
        import httpx
        with patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("network forbidden")):
            result = await self.trial()
        self.assertEqual(result["transcript"], "please change appointment")
        self.assertEqual(result["interruptions"], 1)
        self.assertIsNotNone(result["first_interrupt_ms"])
        self.assertEqual(result["errors"], [])
        self.assertTrue(FakeTranscriber.instances[-1].finished)

    async def test_noise_with_no_transcript_is_not_interrupted(self):
        class Quiet(FakeTranscriber):
            partials = []
            finals = []
        result = await self.trial(Quiet)
        self.assertEqual(result["interruptions"], 0)
        self.assertEqual(result["transcript"], "")

    async def test_echo_is_filtered_by_real_session(self):
        class Echo(FakeTranscriber):
            partials = ["Your options are available tomorrow"]
            finals = partials
        result = await self.trial(Echo)
        self.assertEqual(result["interruptions"], 0)

    async def test_single_partial_does_not_interrupt_but_final_does(self):
        class Partial(FakeTranscriber):
            partials = ["hello"]
            finals = []
        class Final(Partial):
            finals = ["hello"]
        self.assertEqual((await self.trial(Partial))["interruptions"], 0)
        self.assertEqual((await self.trial(Final))["interruptions"], 1)

    async def test_reader_error_notice_is_a_failure(self):
        class ReaderError(FakeTranscriber):
            async def start(self):
                await self.on_notice("elevenlabs_reader_stopped", {"error": "private text"})
        result = await self.trial(ReaderError)
        self.assertIn("elevenlabs_reader_stopped", result["errors"])
        self.assertNotIn("private text", str(result))

    async def test_provider_failure_is_not_a_success_and_cleanup_runs(self):
        class Broken(FakeTranscriber):
            fail = True
        result = await self.trial(Broken)
        self.assertEqual(result["errors"], ["RuntimeError"])
        self.assertNotIn("test error", str(result))
        self.assertTrue(Broken.instances[-1].finished)

    async def test_timeout_is_bounded(self):
        class Slow(FakeTranscriber):
            async def start(self):
                await asyncio.sleep(60)
        result = await run_trial(b"\xff" * 160, "Available tomorrow", factory=Slow,
                                 tail_s=0, timeout_s=0.01)
        self.assertIn("TimeoutError", result["errors"])
        self.assertTrue(Slow.instances[-1].finished)


class CLITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.wav = Path(self.folder.name) / "fixture.wav"
        self.report = Path(self.folder.name) / "report.json"
        with wave.open(str(self.wav), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(tone())
        self.args = ["--speech", str(self.wav), "--reference", "please change appointment",
                     "--noise", f"room={self.wav}", "--report", str(self.report)]
        self.settings = SimpleNamespace(stt_provider="elevenlabs", elevenlabs_api_key="test-only",
                                        barge_in=True, stt_model_label="test", stt_filter_background=True,
                                        stt_endpointing_ms=500, stt_utterance_end_ms=1000)

    async def test_offline_default_never_runs_stt(self):
        with patch("scripts.check_noise.run_trial", new_callable=AsyncMock) as probe:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(await main(self.args), 0)
            probe.assert_not_called()
        report = json.loads(self.report.read_text())
        self.assertEqual(report["mode"], "calibration_only")
        self.assertTrue(all(row["status"] == "not_run" for row in report["trials"]))
        self.assertNotIn("please change appointment", self.report.read_text())

    async def test_existing_report_is_never_overwritten(self):
        self.report.write_text("keep me")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            await main(self.args)
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.report.read_text(), "keep me")

    async def test_live_pass_uses_controls_and_redacts_transcripts(self):
        def result(transcript, count):
            return {"transcript": transcript, "interruptions": count, "first_interrupt_ms": 500,
                    "partials": 1, "errors": []}
        results = [result("please change appointment", 1), result("", 0),
                   result("please change appointment", 1)]
        with patch("src.config.settings", self.settings), patch(
            "scripts.check_noise.run_trial", new_callable=AsyncMock, side_effect=results
        ) as probe, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await main(self.args + ["--live"]), 0)
            self.assertEqual(probe.await_count, 3)
        report = json.loads(self.report.read_text())
        self.assertTrue(all(row["status"] == "pass" for row in report["trials"]))
        self.assertNotIn("please change appointment", self.report.read_text())

    async def test_live_failure_stops_spending_and_returns_nonzero(self):
        result = {"transcript": "", "interruptions": 0, "errors": ["elevenlabs_error"]}
        with patch("src.config.settings", self.settings), patch(
            "scripts.check_noise.run_trial", new_callable=AsyncMock, return_value=result
        ) as probe, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await main(self.args + ["--live"]), 1)
            self.assertEqual(probe.await_count, 1)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["completed_trials"], 1)
        self.assertEqual(report["planned_trials"], 3)

    async def test_missing_provider_key_fails_before_stt(self):
        self.settings.elevenlabs_api_key = ""
        with patch("src.config.settings", self.settings), patch(
            "scripts.check_noise.run_trial", new_callable=AsyncMock
        ) as probe, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            await main(self.args + ["--live"])
        self.assertEqual(error.exception.code, 2)
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
