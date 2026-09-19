from __future__ import annotations

import argparse
import asyncio
import base64
import json
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
import websockets
from fastapi.responses import HTMLResponse

from v2.api import create_app
from v2.config import Config
from v2.store import RunStore


class Browser:
    def __init__(self, connection):
        self.connection = connection
        self.sequence = 0
        self.exceptions = []

    async def command(self, method, **params):
        self.sequence += 1
        identifier = self.sequence
        await self.connection.send(json.dumps({"id": identifier, "method": method, "params": params}))
        async with asyncio.timeout(15):
            while True:
                message = json.loads(await self.connection.recv())
                if message.get("method") == "Runtime.exceptionThrown":
                    self.exceptions.append(message["params"])
                if message.get("id") == identifier:
                    if "error" in message:
                        raise AssertionError("browser protocol command failed")
                    return message.get("result", {})

    async def evaluate(self, expression, *, user_gesture=False):
        result = await self.command("Runtime.evaluate", expression=expression, returnByValue=True,
                                    awaitPromise=True, userGesture=user_gesture)
        if "exceptionDetails" in result:
            raise AssertionError("browser script evaluation failed")
        return result.get("result", {}).get("value")

    async def until(self, expression):
        async with asyncio.timeout(15):
            while not await self.evaluate(expression):
                await asyncio.sleep(0.05)


async def inspect_voice(browser: Browser, port: int, token: str):
    await browser.command("Page.addScriptToEvaluateOnNewDocument", source="""
        window.testStreams = []; window.testContexts = []; window.testVoiceRequests = [];
        const request = window.fetch.bind(window);
        window.fetch = (url, options) => {
            if (url === '/api/voice/ticket' || url === '/api/public/voice/ticket') window.testVoiceRequests.push({path: url, body: JSON.parse(options.body), operatorHeader: Object.hasOwn(options.headers, 'X-V2-Token')});
            return request(url, options);
        };
        const capture = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        navigator.mediaDevices.getUserMedia = async options => {
            const stream = await capture(options); window.testStreams.push(stream); return stream;
        };
        const Context = window.AudioContext;
        window.AudioContext = class extends Context {
            constructor(...args) { super(...args); this.playbackStarts = 0; window.testContexts.push(this); }
            createBufferSource() {
                const source = super.createBufferSource(), start = source.start.bind(source);
                source.start = (...args) => { this.playbackStarts++; return start(...args); };
                return source;
            }
        };
    """)
    await browser.command("Page.navigate", url=f"http://127.0.0.1:{port}/")
    await browser.until("!document.querySelector('#start-voice')?.disabled")
    assert await browser.evaluate("document.featurePolicy.allowsFeature('microphone')")
    assert await browser.evaluate("document.querySelector('#auth-panel').hidden && document.querySelector('#logout').hidden && !document.querySelector('#view-talk').hidden && testStreams.length === 0 && document.querySelector('#call-options').hidden && document.querySelector('#inspect-session').hidden")
    assert await browser.evaluate("fetch('/api/runs', {headers: {'ngrok-skip-browser-warning': '1'}}).then(r => r.status === 401)")
    await browser.evaluate("document.querySelector('#start-voice').click();", user_gesture=True)
    await browser.until("document.querySelector('#microphone-state').textContent === 'Microphone live'")
    assert await browser.evaluate("testVoiceRequests.length === 1 && testVoiceRequests[0].path === '/api/public/voice/ticket' && Object.keys(testVoiceRequests[0].body).length === 0 && !testVoiceRequests[0].operatorHeader")
    await browser.until("document.querySelector('#microphone-level').value > 0 && testContexts[0].playbackStarts > 0")
    assert await browser.evaluate("!document.querySelector('#voice-controls').hidden && document.querySelector('#turn-form').hidden && testStreams[0].getTracks().every(t => t.readyState === 'live' && t.enabled)")
    await browser.evaluate("document.querySelector('#mute-voice').click()", user_gesture=True)
    await browser.until("document.querySelector('#microphone-state').textContent === 'Microphone muted' && document.querySelector('#microphone-level').value === 0")
    assert await browser.evaluate("testStreams[0].getTracks().every(t => !t.enabled) && testContexts[0].state === 'running' && document.querySelector('#mute-voice').getAttribute('aria-pressed') === 'true'")
    await browser.evaluate("document.querySelector('#mute-voice').click()", user_gesture=True)
    await browser.until("document.querySelector('#microphone-level').value > 0 && testStreams[0].getTracks().every(t => t.enabled)")
    await browser.evaluate("document.querySelector('#stop-session').click()")
    await browser.until("document.querySelector('#voice-controls').hidden && testStreams[0].getTracks().every(t => t.readyState === 'ended') && testContexts[0].state === 'closed'")
    assert await browser.evaluate("document.querySelector('#microphone-level').value === 0 && !document.querySelector('#start-voice').disabled")
    await browser.until("fetch('/health', {headers: {'ngrok-skip-browser-warning': '1'}}).then(r => r.json()).then(h => h.active_voice_calls === 0)")
    await browser.evaluate("document.querySelector('#start-voice').click()", user_gesture=True)
    await browser.until("testStreams.length === 2 && document.querySelector('#microphone-state').textContent === 'Microphone live'")
    assert await browser.evaluate("testVoiceRequests.every(r => !r.operatorHeader && r.path === '/api/public/voice/ticket')")
    await browser.evaluate("document.querySelector('#operator-login').click()")
    await browser.until("!document.querySelector('#auth-panel').hidden")
    await browser.evaluate(f"document.querySelector('#token').value={json.dumps(token)}; document.querySelector('#auth-form').requestSubmit();")
    await browser.until("!document.querySelector('#logout').hidden")
    await browser.evaluate("document.querySelector('#logout').click()")
    await browser.until("!document.querySelector('#auth-panel').hidden && testStreams.every(s => s.getTracks().every(t => t.readyState === 'ended')) && testContexts.every(c => c.state === 'closed')")
    await browser.evaluate("location.hash='#talk'")
    await browser.until("document.querySelector('#auth-panel').hidden && !document.querySelector('#view-talk').hidden")
    assert await browser.evaluate("document.querySelector('#voice-controls').hidden && !document.querySelector('#start-voice').disabled && document.querySelector('#call-options').hidden && localStorage.length === 0 && sessionStorage.length === 0")


async def inspect(browser_path: str, root: Path, port: int, token: str, voice_port: int):
    profile = root / "browser-profile"
    process = subprocess.Popen([browser_path, "--headless=new", "--disable-gpu", "--no-first-run",
                                "--no-default-browser-check", "--remote-debugging-port=0",
                                "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                                f"--user-data-dir={profile}", "about:blank"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with asyncio.timeout(20):
            while not (profile / "DevToolsActivePort").is_file():
                if process.poll() is not None:
                    raise RuntimeError("headless browser exited before readiness")
                await asyncio.sleep(0.05)
        debug_port = int((profile / "DevToolsActivePort").read_text().splitlines()[0])
        async with httpx.AsyncClient(trust_env=False) as http:
            pages = (await http.get(f"http://127.0.0.1:{debug_port}/json/list")).json()
        target = next(page for page in pages if page.get("type") == "page")
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=8_000_000) as connection:
            browser = Browser(connection)
            await browser.command("Runtime.enable")
            await browser.command("Page.enable")
            await browser.command("Emulation.setDeviceMetricsOverride", width=1440, height=1000,
                                  deviceScaleFactor=1, mobile=False)
            await browser.command("Page.navigate", url=f"http://127.0.0.1:{port}/#overview")
            await browser.until("document.querySelector('#backend-status')?.textContent === 'Reachable'")
            await browser.evaluate("document.querySelector('#token').value='invalid-fixture-token'; document.querySelector('#auth-form').requestSubmit();")
            await browser.until("!document.querySelector('#notice').hidden && !document.querySelector('#unlock').disabled")
            assert await browser.evaluate("!document.querySelector('#auth-panel').hidden")
            await browser.evaluate(f"document.querySelector('#token').value={json.dumps(token)}; document.querySelector('#auth-form').requestSubmit();")
            await browser.until("document.querySelector('#auth-panel').hidden && document.querySelector('#run-count').textContent === '0'")
            for language in ("en", "es", "ca"):
                await browser.evaluate(f"location.hash='#talk'; document.querySelector('#call-options').open=true; document.querySelector('#language').value='{language}'; document.querySelector('#fixture').click();")
                await browser.until(f"document.querySelector('#trace').textContent && JSON.parse(document.querySelector('#trace').textContent).state.language === '{language}' && !document.querySelector('#fixture').disabled")
                assert await browser.evaluate("(() => { const r=JSON.parse(document.querySelector('#trace').textContent); return r.metrics.resolved === 2 && r.fixture_grade.passed && r.official_grade === null && r.mode === 'simulation'; })()")
                assert await browser.evaluate("document.querySelectorAll('#transcript .turn').length >= 4")
            await browser.evaluate("location.hash='#talk'")
            assert await browser.evaluate("document.querySelector('#start-voice').disabled && document.querySelector('#start-text').disabled")
            for width, height in ((1440, 1000), (390, 844)):
                await browser.command("Emulation.setDeviceMetricsOverride", width=width, height=height,
                                      deviceScaleFactor=1, mobile=width < 600)
                for view in ("overview", "runs", "talk"):
                    await browser.evaluate(f"location.hash='#{view}'")
                    await browser.until(f"!document.querySelector('#view-{view}').hidden")
                    assert await browser.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"horizontal overflow in {view} at {width}px"
            await browser.evaluate("location.hash='#overview'; document.querySelector('#logout').click()")
            await browser.until("!document.querySelector('#auth-panel').hidden && document.querySelector('#trace').textContent === ''")
            assert await browser.evaluate("localStorage.length === 0 && sessionStorage.length === 0 && document.querySelectorAll('audio[src]').length === 0")
            await inspect_voice(browser, voice_port, token)
            assert not browser.exceptions, "uncaught browser exception"
            await browser.command("Browser.close")
        await asyncio.to_thread(process.wait, 10)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 10)


async def mock_voice(socket, stream_sid, controller, config):
    received = 0
    payload = base64.b64encode(bytes([0x80, 0x00]) * 80).decode()
    await socket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})
    try:
        while True:
            message = await socket.receive_json()
            if message.get("event") == "stop":
                break
            if message.get("event") == "media":
                data = base64.b64decode(message["media"]["payload"], validate=True)
                assert len(data) == 160
                received += 1
                await socket.send_json({"event": "media", "streamSid": stream_sid,
                                        "media": {"payload": message["media"]["payload"]}})
    finally:
        controller.store.event(controller.state.run_id, "mock_voice_capture", {"frames_received": received, "synthetic": True})


@contextmanager
def serve(config: Config, store: RunStore):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    app = create_app(config, store)
    @app.middleware("http")
    async def ngrok_interstitial(request, call_next):
        if (request.url.path == "/health" or request.url.path.startswith("/api/")) and request.headers.get("ngrok-skip-browser-warning") != "1":
            return HTMLResponse("<html><body>ngrok browser warning fixture</body></html>")
        return await call_next(request)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.05)
        yield port
    finally:
        server.should_exit = True
        thread.join(10)


def main():
    parser = argparse.ArgumentParser(description="Offline Chrome/Edge operator and mocked microphone smoke; no real providers or private data.")
    parser.add_argument("--browser", default=shutil.which("chrome") or shutil.which("msedge"))
    args = parser.parse_args()
    if not args.browser:
        parser.error("supply --browser with the Chrome or Edge executable")
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        token = secrets.token_urlsafe(24)
        config = Config(operator_token=token, allow_paid=False, mode="simulation", data_dir=root,
                        gateway_key="", deepgram_key="", cartesia_key="", elevenlabs_key="", api_key="",
                        allow_submissions=False, release_approved=False, jev_url="", jev_token="", public_browser_calls=False)
        voice_config = replace(config, allow_paid=True, public_browser_calls=True, gateway_key="mock-gateway", deepgram_key="mock-stt",
                               cartesia_key="mock-tts", elevenlabs_key="mock-ca", voice_es="mock-es", api_key="mock-clinic")
        store, voice_store = RunStore(), RunStore()
        try:
            with patch("v2.voice.run_voice", side_effect=mock_voice), serve(config, store) as port, serve(voice_config, voice_store) as voice_port:
                asyncio.run(inspect(args.browser, root, port, token, voice_port))
            assert store.budget()["committed_microusd"] == voice_store.budget()["committed_microusd"] == 0
            assert len(store.list_runs()["runs"]) == 3
            runs = voice_store.list_runs()["runs"]
            assert len(runs) == 2
            captured = [event["payload"]["frames_received"] for run in runs
                        for event in voice_store.report(run["run_id"])["events"] if event["kind"] == "mock_voice_capture"]
            assert captured and max(captured) > 0
            print("PASS: real-browser authentication, three-language fixtures, paid gating, responsive layout, fake microphone/WebSocket playback, mute, hangup and logout; mocked providers; spend $0")
        finally:
            store.close()
            voice_store.close()


if __name__ == "__main__":
    main()
