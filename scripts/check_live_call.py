"""Paid end-to-end audio check against a local server; never submits to Prosper."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
import websockets
from src.voice.tts import ElevenLabsSynthesizer


async def run(base: str) -> int:
    voice = ElevenLabsSynthesizer()
    try:
        caller = b''.join([c async for c in voice.stream(
            'Hello, what time does Arenal Sur close on Fridays?', 'en')])
    finally:
        await voice.aclose()
    call_id = 'audit-' + uuid.uuid4().hex
    url = base.replace('http://', 'ws://').replace('https://', 'wss://') + '/ws'
    received = 0
    async with websockets.connect(url) as socket:
        async def receive():
            nonlocal received
            async for raw in socket:
                if json.loads(raw).get('event') == 'media':
                    received += 1
        reader = asyncio.create_task(receive())
        try:
            await socket.send(json.dumps({'event':'start', 'start':{
                'callSid':call_id, 'streamSid':call_id,
                'customParameters':{'dry_run':'true'},
                'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}}))
            # Let the greeting finish, then speak and leave time for the answer.
            audio = b'\xff'*(8000*5) + caller + b'\xff'*(8000*14)
            for pos in range(0,len(audio),160):
                await socket.send(json.dumps({'event':'media','media':{
                    'track':'inbound','payload':base64.b64encode(audio[pos:pos+160]).decode()}}))
                await asyncio.sleep(.02)
            await socket.send(json.dumps({'event':'stop'}))
            await asyncio.wait_for(reader, 12)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    async with httpx.AsyncClient() as http:
        r = await http.get(base + '/api/console/calls/' + call_id)
        r.raise_for_status()
        detail = r.json()
    for turn in detail.get('transcript', []):
        print(turn.get('role'), turn.get('text'))
    callers = [t for t in detail.get('transcript',[]) if t.get('role') == 'caller']
    # The console calls the inbound role "caller" or "patient" across versions.
    if not callers:
        callers = [t for t in detail.get('transcript',[]) if t.get('role') != 'agent']
    agents = [t for t in detail.get('transcript',[]) if t.get('role') == 'agent']
    errors = detail.get('errors', [])
    print('caller_turns:',len(callers), 'agent_lines:',len(agents),
          'audio_frames:',received, 'errors:',errors)
    ok = bool(callers) and len(agents)>1 and received>0 and not errors
    print('PASS' if ok else 'FAIL', call_id)
    return int(not ok)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--console', default='http://127.0.0.1:7861')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.console.rstrip('/'))))
