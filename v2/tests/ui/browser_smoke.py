from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import websockets

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

    async def evaluate(self, expression):
        result = await self.command("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise AssertionError("browser script evaluation failed")
        return result.get("result", {}).get("value")

    async def until(self, expression):
        async with asyncio.timeout(15):
            while not await self.evaluate(expression):
                await asyncio.sleep(0.05)


async def inspect(browser_path: str, root: Path, port: int, token: str):
    profile = root / "browser-profile"
    process = subprocess.Popen([browser_path, "--headless=new", "--disable-gpu", "--no-first-run",
                                "--no-default-browser-check", "--remote-debugging-port=0",
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
            await browser.command("Page.navigate", url=f"http://127.0.0.1:{port}/")
            await browser.until("document.querySelector('#backend-status')?.textContent === 'Reachable'")
            await browser.evaluate("document.querySelector('#token').value='invalid-fixture-token'; document.querySelector('#auth-form').requestSubmit();")
            await browser.until("!document.querySelector('#notice').hidden && !document.querySelector('#unlock').disabled")
            assert await browser.evaluate("!document.querySelector('#auth-panel').hidden")
            await browser.evaluate(f"document.querySelector('#token').value={json.dumps(token)}; document.querySelector('#auth-form').requestSubmit();")
            await browser.until("document.querySelector('#auth-panel').hidden && document.querySelector('#run-count').textContent === '0'")
            for language in ("en", "es", "ca"):
                await browser.evaluate(f"location.hash='#talk'; document.querySelector('#language').value='{language}'; document.querySelector('#fixture').click();")
                await browser.until(f"document.querySelector('#trace').textContent && JSON.parse(document.querySelector('#trace').textContent).state.language === '{language}' && !document.querySelector('#fixture').disabled")
                assert await browser.evaluate("(() => { const r=JSON.parse(document.querySelector('#trace').textContent); return r.metrics.resolved === 2 && r.fixture_grade.passed && r.official_grade === null && r.mode === 'simulation'; })()")
                assert await browser.evaluate("document.querySelectorAll('#transcript .turn').length >= 4")
            await browser.evaluate("location.hash='#talk'; document.querySelector('#paid-consent').click(); document.querySelector('#start-text').click();")
            await browser.until("document.querySelector('#session-status').textContent.startsWith('Session start failed')")
            assert await browser.evaluate("document.querySelector('#start-voice').disabled")
            for width, height in ((1440, 1000), (390, 844)):
                await browser.command("Emulation.setDeviceMetricsOverride", width=width, height=height,
                                      deviceScaleFactor=1, mobile=width < 600)
                for view in ("overview", "runs", "talk"):
                    await browser.evaluate(f"location.hash='#{view}'")
                    await browser.until(f"!document.querySelector('#view-{view}').hidden")
                    assert await browser.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"horizontal overflow in {view} at {width}px"
            await browser.evaluate("document.querySelector('#logout').click()")
            await browser.until("!document.querySelector('#auth-panel').hidden && document.querySelector('#trace').textContent === ''")
            assert await browser.evaluate("localStorage.length === 0 && sessionStorage.length === 0 && document.querySelectorAll('audio[src]').length === 0")
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


def main():
    parser = argparse.ArgumentParser(description="Offline Chrome/Edge operator smoke; no paid providers or private data.")
    parser.add_argument("--browser", default=shutil.which("chrome") or shutil.which("msedge"))
    args = parser.parse_args()
    if not args.browser:
        parser.error("supply --browser with the Chrome or Edge executable")
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        token = secrets.token_urlsafe(24)
        config = Config(operator_token=token, allow_paid=False, mode="simulation", data_dir=root,
                        gateway_key="", deepgram_key="", cartesia_key="", elevenlabs_key="", api_key="")
        store = RunStore()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(config, store), host="127.0.0.1", port=port,
                                               log_level="critical", access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("test server did not start")
                time.sleep(0.05)
            asyncio.run(inspect(args.browser, root, port, token))
            assert store.budget()["committed_microusd"] == 0
            assert len(store.list_runs()["runs"]) == 3
            print("PASS: real-browser authentication, three-language fixtures, run inspection, paid gating, desktop/mobile layout, and logout; spend $0")
        finally:
            server.should_exit = True
            thread.join(10)
            store.close()


if __name__ == "__main__":
    main()
