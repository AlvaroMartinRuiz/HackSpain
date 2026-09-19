"""Offline regressions for lost speech, interruption and concurrent calls."""
from __future__ import annotations

import asyncio
import base64
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from src.config import settings
from src.domain.catalog import Catalog
from src.agent.llm import LLMClient
from src.agent.brain import TurnInterrupted
from src.telephony.session import CallSession
from src.voice.stt import DeepgramTranscriber, ElevenLabsTranscriber
from src.voice.turns import TurnGate


class PipelineChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.llm = LLMClient()
        self.session = CallSession('offline-audit', 'stream', None, AsyncMock(),
                                   Catalog.load(), SimpleNamespace(record=AsyncMock()),
                                   self.llm, text_mode=True, dry_run=True)
        self.session.agent.handle = AsyncMock()

    async def asyncTearDown(self):
        self.session._disarm_turn_deadline()
        self.session._disarm_silence()
        for task in self.session._tasks:
            task.cancel()
        await asyncio.gather(*self.session._tasks, return_exceptions=True)
        await self.session.client.aclose()
        await self.llm.aclose()

    async def test_low_confidence_final_is_not_lost(self):
        final, notice = AsyncMock(), AsyncMock()
        stt = DeepgramTranscriber(AsyncMock(), final, on_notice=notice)
        await stt._handle({'type':'Results', 'is_final':True, 'speech_final':True,
                          'channel':{'alternatives':[{'transcript':'48924647',
                                                     'confidence':0.1}]}})
        final.assert_awaited_once_with('48924647', None)
        self.assertEqual(notice.await_args.args[0], 'stt_low_confidence')

    async def test_scribe_delayed_metadata_does_not_duplicate_turns(self):
        final = AsyncMock()
        stt = ElevenLabsTranscriber(AsyncMock(), final)
        await stt._handle({'message_type':'committed_transcript', 'text':'Yes.'})
        await stt._handle({'message_type':'partial_transcript', 'text':'And'})
        await stt._handle({'message_type':'committed_transcript_with_timestamps',
                          'text':'Yes.', 'language_code':'eng'})
        final.assert_awaited_once_with('Yes.', 'en')
        await stt._handle({'message_type':'committed_transcript', 'text':'Yes.'})
        await stt._handle({'message_type':'committed_transcript_with_timestamps',
                          'text':'Yes.', 'language_code':'eng'})
        self.assertEqual(final.await_count, 2)
        await stt._handle({'message_type':'committed_transcript_with_timestamps',
                          'text':'Yes.', 'language_code':'eng'})
        await stt._handle({'message_type':'committed_transcript', 'text':'Yes.'})
        self.assertEqual(final.await_count, 3)
        self.assertEqual(stt._pending_commits, [])

    async def test_answer_repeating_agent_is_kept(self):
        self.session._last_spoken = 'Would you prefer Monday morning?'
        await self.session.feed_text('Monday morning.')
        self.session.agent.handle.assert_awaited_once_with('Monday morning.')

    async def test_repeated_confirmations_are_distinct_turns(self):
        await self.session.feed_text('Yes.')
        await self.session.feed_text('Yes.')
        self.assertEqual(self.session.agent.handle.await_count, 2)

    async def test_outbound_audio_is_never_transcribed(self):
        self.session.transcriber = SimpleNamespace(push=AsyncMock())
        payload = base64.b64encode(b'\xff'*160).decode()
        await self.session.on_media(payload, 'outbound')
        self.session.transcriber.push.assert_not_awaited()
        await self.session.on_media(payload, 'inbound')
        self.session.transcriber.push.assert_awaited_once()

    async def test_stt_reader_does_not_wait_for_llm(self):
        self.session.text_mode = False
        running, release = asyncio.Event(), asyncio.Event()
        async def handle(_text):
            running.set()
            await release.wait()
        self.session.agent.handle = handle
        await asyncio.wait_for(self.session._on_final('I need an appointment', 'en'), .5)
        await asyncio.wait_for(running.wait(), .5)
        await asyncio.wait_for(self.session._on_partial('No, wait!'), .5)
        self.assertGreater(self.session._generation, 0)
        release.set()
        await self.session._settled()

    async def test_late_stt_after_smart_turn_complete(self):
        self.session.text_mode = False
        self.session._turn_gate = SimpleNamespace(enabled=True, turn_over=True,
                                                   speaking=False, reset=lambda: None)
        await self.session._on_turn_event('complete')
        await self.session._on_final('Monday', 'en')
        await self.session._on_final('morning please', 'en')
        await asyncio.sleep(.5)
        await self.session._settled()
        self.session.agent.handle.assert_awaited_once_with('Monday morning please')

    async def test_deadline_does_not_cut_active_speech(self):
        self.session._turn_gate = SimpleNamespace(speaking=True)
        self.session._turn_parts = ['I would like']
        await self.session._turn_deadline(0)
        self.session.agent.handle.assert_not_awaited()
        self.assertEqual(self.session._turn_parts, ['I would like'])

    async def test_short_interruption_with_punctuation(self):
        for word in ('No!', 'Wait.', 'Stop!', 'Espera.', 'S\u00ed.'):
            self.assertTrue(self.session._worthy_barge_in(word, final=False), word)
        self.assertFalse(self.session._worthy_barge_in('um', final=False))

    async def test_stale_model_text_is_not_spoken(self):
        async def stream(*args):
            self.session._generation += 1
            yield 'text', 'The old answer.'
        self.session.agent.llm = SimpleNamespace(stream=stream)
        with self.assertRaises(TurnInterrupted):
            await self.session.agent._run_round(self.session._generation)
        self.assertTrue(self.session._say_queue.empty())

    async def test_filler_waits_for_caller(self):
        self.session.text_mode = False
        self.session._caller_speaking = True
        self.session.say = AsyncMock()
        with patch('src.agent.brain.HOLD_IF_QUIET_S', 0):
            await self.session.agent._hold_if_quiet()
        self.session.say.assert_not_awaited()

    async def test_turn_gate_executor_closes(self):
        from unittest.mock import Mock
        gate = TurnGate.__new__(TurnGate)
        executor = Mock()
        gate._analyzer = SimpleNamespace(_executor=executor)
        await gate.aclose()
        await gate.aclose()
        executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    async def test_llm_requests_overlap(self):
        active = 0
        both = asyncio.Event()
        async def respond(request):
            nonlocal active
            active += 1
            if active == 2:
                both.set()
            await asyncio.wait_for(both.wait(), .8)
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        await self.llm._client.aclose()
        self.llm._client = httpx.AsyncClient(transport=httpx.MockTransport(respond),
                                            base_url='https://test.invalid')
        async def call():
            return [item async for item in self.llm.stream([{'role':'user','content':'Hi'}])]
        with patch('src.agent.llm.settings', replace(settings, llm_min_gap_s=0)):
            results = await asyncio.gather(call(), call())
        self.assertEqual([r[-1][1].text for r in results], ['OK', 'OK'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
