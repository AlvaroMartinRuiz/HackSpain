"""Checks for the call-handling fixes: audio pacing, barge-in, silence prompts,
the voice cache, the console guard, and the tool-level rules.

No model and no voice minutes: a fake synthesiser stands in for TTS, sessions
run with dry_run so nothing is submitted, and the only network use is two
read-only directory lookups against the clinic.

  .venv/Scripts/python scripts/check_fixes.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from src.config import settings  # noqa: E402

# Frozen settings, overridden for the checks only: short silences, a known token.
object.__setattr__(settings, "silence_prompt_s", 0.8)
object.__setattr__(settings, "console_token", "check-token")

from src.agent.llm import LLMClient  # noqa: E402
from src.agent.tools import _OTHER_PLAN_QUESTION, _spell_email  # noqa: E402
from src.domain.catalog import Catalog  # noqa: E402
from src.domain.identity import dni_check_letter, normalize_text, parse_national_id  # noqa: E402
from src.obs.store import store  # noqa: E402
from src.telephony import session as session_module  # noqa: E402
from src.telephony.session import CallSession, _two_letter  # noqa: E402

# The opening re-ask (nobody has spoken yet) has its own fixed wait.
session_module.SILENCE_OPENING_RETRY_S = 0.8
from src.voice import tts  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: Any = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail != "" else ""))
    if not ok:
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


class FakeTTS(tts.Synthesizer):
    """Seconds of µ-law after a delay, or an error; never a network call."""

    def __init__(self, seconds: float = 2.0, delay: float = 0.3, fail: bool = False) -> None:
        self.seconds, self.delay, self.fail = seconds, delay, fail
        self.calls = 0

    async def stream(self, text: str, language: str = "es"):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("fake tts HTTP 500")
        total = int(8000 * self.seconds)
        for i in range(0, total, 1600):
            yield b"\xff" * min(1600, total - i)


CATALOG = Catalog.load(Path(settings.catalog_path))


def new_session(text_mode: bool = False) -> tuple[CallSession, list[float]]:
    sent: list[float] = []

    async def send(message: dict[str, Any]) -> None:
        if message.get("event") == "media":
            sent.append(time.monotonic())

    call_id = f"check-{time.time_ns()}"
    store.open_call(call_id, None)
    session = CallSession(call_id, "MZcheck", None, send, CATALOG, store, LLMClient(),
                          text_mode=text_mode, dry_run=True)
    return session, sent


def speak(session: CallSession, synth: tts.Synthesizer) -> None:
    session.synthesizer = synth
    session._tasks = [asyncio.create_task(session._speaker_loop()),
                      asyncio.create_task(session._player_loop())]


def stop(session: CallSession) -> None:
    session._disarm_silence()
    for task in session._tasks:
        task.cancel()


def agent_lines(session: CallSession) -> list[str]:
    call = store.get(session.call_id)
    return [t["text"] for t in call.detail()["transcript"] if t.get("role") == "agent"] if call else []


async def audio_checks() -> None:
    section("A1 · audio goes out in real time, and barge-in works during synthesis")
    session, sent = new_session()
    speak(session, FakeTTS(seconds=2.0, delay=0.3))
    await session.say("first reply")
    await asyncio.sleep(3.0)
    before = len(sent)
    await session.say("second reply after a pause")
    await asyncio.sleep(0.8)
    early = len(sent) - before
    # 0.5 s of audio after the 0.3 s synthesis, plus the 200 ms lead, at 20 ms a frame.
    check("a reply after a pause is not sent in a burst", early <= 50,
          f"{early} frames in 0.8 s (a burst would be ~100)")
    stop(session)

    session, sent = new_session()
    speak(session, FakeTTS(seconds=2.0, delay=1.5))
    await session.say("a sentence still being synthesised")
    # Cut in once synthesis is under way, not after a fixed delay: a cold start
    # (first database write) can otherwise let the audio begin first.
    for _ in range(100):
        if session._synthesizing:
            break
        await asyncio.sleep(0.01)
    generation = session._generation
    await session._on_partial("no wait, I meant Thursday instead")
    await asyncio.sleep(1.5)
    check("a caller cutting in during synthesis stops the reply",
          session._generation == generation + 1 and not sent, f"{len(sent)} frames sent")
    stop(session)


async def silence_checks() -> None:
    section("A2 · 'are you still there?' after silence")
    session, _ = new_session()
    speak(session, FakeTTS(seconds=0.3, delay=0.05))
    await session.say("How can I help?")
    await asyncio.sleep(1.5)
    check("nobody has spoken yet: the opening re-ask, in English",
          any("anyone there" in t for t in agent_lines(session)), agent_lines(session)[-1:])
    stop(session)

    session, _ = new_session()
    session.language = "en"
    session._heard_caller = True
    speak(session, FakeTTS(seconds=0.3, delay=0.05))
    await session.say("How can I help?")
    await asyncio.sleep(4.5)
    prompts = [t for t in agent_lines(session) if "there" in t or "hear me" in t]
    check("after the caller has spoken: two prompts, then quiet", len(prompts) == 2, prompts)
    history = [m["content"] for m in session.agent.messages if m["role"] == "assistant"]
    check("the prompt is in the model's history", bool(history) and "hear me" in history[-1])
    stop(session)

    session, _ = new_session()
    speak(session, FakeTTS(seconds=0.3, delay=0.05))
    await session.say("How can I help?")
    await asyncio.sleep(0.9)
    await session._on_partial("yes hello I wanted")
    await asyncio.sleep(1.2)
    check("no prompt when the caller starts talking", session._silence_prompts == 0)
    stop(session)

    session, _ = new_session()
    speak(session, FakeTTS(seconds=0.3, delay=0.05))
    await session.say("How can I help?")
    await asyncio.sleep(0.5)
    async with session._turn_lock:
        await asyncio.sleep(1.5)
    check("no prompt while the model is thinking", session._silence_prompts == 0)
    stop(session)

    session, _ = new_session()
    session.language = "es"
    session._heard_caller = True
    speak(session, FakeTTS(seconds=0.2, delay=0.05))
    await session.say("¿En qué puedo ayudarle?")
    await asyncio.sleep(1.6)
    check("the prompt follows the call's language", "¿Sigue ahí?" in agent_lines(session),
          agent_lines(session)[-1:])
    stop(session)


async def cache_checks() -> None:
    section("B1 · fixed lines play from the cache")
    fake = FakeTTS(seconds=0.5, delay=0.05)
    text = settings.greeting
    tts.remember_audio(text, "en", b"\xff" * 4000)
    session, sent = new_session()
    session.language = "en"
    speak(session, fake)
    await session.say(text, first=True)
    await asyncio.sleep(0.4)
    check("the greeting plays without calling the provider", fake.calls == 0 and bool(sent),
          f"provider calls {fake.calls}, frames {len(sent)}")
    stop(session)
    check("a line that is not fixed is never cached", not tts.is_fixed_line("Your appointment is on Thursday"))
    check("each provider has its own gate",
          tts._GATES["ElevenLabsSynthesizer"] is not tts._GATES["DeepgramSynthesizer"])


async def tool_checks() -> None:
    section("L1 · an insurance refusal waits for the second-plan question")
    session, _ = new_session(text_mode=True)
    tools = session.agent.tools
    tools.patient = {"patient_id": "P00001", "insurer": "asisa"}
    result = await tools.dispatch("end_without_booking", {"reason": "specialty_not_covered"})
    check("held when nobody asked", result.get("error") == "ask_second_plan" and not session.submissions)
    result = await tools.dispatch("end_without_booking", {"reason": "specialty_not_covered",
                                                          "caller_has_no_other_plan": True})
    check("still held when the model claims it asked but never did",
          result.get("error") == "ask_second_plan" and not session.submissions)
    session.agent.messages.append({"role": "assistant", "content": "Do you have any other insurance?"})
    result = await tools.dispatch("end_without_booking", {"reason": "specialty_not_covered",
                                                          "caller_has_no_other_plan": True})
    check("sent once it asked and the caller has none",
          result.get("recorded") is True and session.submissions[-1].payload["reason"] == "specialty_not_covered")
    other, _ = new_session(text_mode=True)
    other.agent.tools.patient = {"patient_id": "P00001"}
    result = await other.agent.tools.dispatch("end_without_booking", {"reason": "no_availability"})
    check("a refusal that is not about insurance is not held", result.get("recorded") is True)
    asks = ["¿Tiene otro seguro que podamos considerar?", "Do you hold another plan?",
            "Té alguna altra assegurança?"]
    nots = ["Your plan does not cover physiotherapy.", "¿Quiere otra cita?", "Tengo otra opción el jueves."]
    check("the question is recognised in en/es/ca",
          all(_OTHER_PLAN_QUESTION.search(normalize_text(t)) for t in asks))
    check("and not confused with other sentences",
          not any(_OTHER_PLAN_QUESTION.search(normalize_text(t)) for t in nots))

    section("L3 · a national id without its letter")
    parsed = parse_national_id("48924647")
    check("the letter is derived and flagged",
          parsed["letter_missing"] and parsed["value"] == "48924647" + dni_check_letter("48924647"))
    wrong = "A" if dni_check_letter("48924647") != "A" else "B"
    check("a contradicting letter is not treated as missing",
          not parse_national_id("48924647" + wrong)["letter_missing"])
    alone = await session.engine.identify(national_id="48924647")
    check("on its own it searches nobody and asks for a second field",
          alone["needs_more"] and alone["count"] == 0)
    matches = await session.client.directory(name="Marta Ruiz")
    real = next((m for m in matches if m.get("national_id")), None)
    if real:
        found = await session.engine.identify(name=real["given_name"] + " " + real["first_surname"],
                                              national_id=real["national_id"][:-1])
        check("with the name beside it, the right patient is found",
              any(m["patient_id"] == real["patient_id"] for m in found["matches"]))

    section("L2 · a registration is read back before it is sent")
    session, _ = new_session(text_mode=True)
    tools = session.agent.tools
    fields = {"given_name": "Nuria", "first_surname": "Delgado", "second_surname": "Domínguez",
              "national_id": "48924647", "date_of_birth": "1974-06-12", "phone": "680071273",
              "email": "nuria underscore delgado86 at outlook dot es", "insurer": "axa"}
    result = await tools.dispatch("register_new_patient", fields)
    check("the first call sends nothing", result.get("needs_confirmation") and not session.submissions)
    check("names are spelled", result["read_back"]["given_name"] == "N-U-R-I-A")
    check("an inferred letter is flagged", result.get("letter_inferred") is True)
    corrected = {**fields, "given_name": "Núria", "confirmed": True}
    result = await tools.dispatch("register_new_patient", corrected)
    check("a correction goes back to the read-back", result.get("needs_confirmation") and not session.submissions)
    result = await tools.dispatch("register_new_patient", corrected)
    check("confirmed after the read-back, it is sent",
          result.get("registered") and session.submissions[-1].payload["given_name"] == "Núria")
    check("the email is spelled in the call's language",
          "arroba" in _spell_email("a.b@x.es", "es") and " at " in _spell_email("a.b@x.es", "en"))

    section("B3 · language")
    check("transcriber codes become two letters",
          [_two_letter(c) for c in ("spa", "cat", "eng", "ca-ES")] == ["es", "ca", "en", "ca"])
    session, _ = new_session(text_mode=True)
    tools = session.agent.tools
    patient = (await session.client.directory(name="Marta Ruiz"))[0]
    await tools.dispatch("open_chart", {"patient_id": patient["patient_id"]})
    session.language = "ca"
    result = await tools.dispatch("find_appointments", {"patient_id": patient["patient_id"],
                                                        "specialty_id": "general_practice"})
    speakers = {pid for pid, p in CATALOG.providers.items() if p.speaks("ca")}
    offered = {o["provider_id"] for o in result.get("options", [])}
    check("a Catalan caller is offered only Catalan-speaking doctors",
          bool(offered) and offered <= speakers, sorted(offered))

    section("A template slot is never spoken")
    session, _ = new_session(text_mode=True)
    await session.say("¿Hablo con [nombre del paciente]?")
    await session.say("¿Hablo con Rosa Delgado?")
    spoken = agent_lines(session)
    check("the placeholder line is suppressed, the real one is not",
          spoken == ["¿Hablo con Rosa Delgado?"], spoken)

    section("Safety net")
    session, _ = new_session(text_mode=True)
    check("nobody looked up: out_of_scope", session.agent.tools.fallback_reason() == "out_of_scope")
    session.agent.tools.lookup_attempted = True
    check("looked up and not found: patient_not_found",
          session.agent.tools.fallback_reason() == "patient_not_found")


def guard_checks() -> None:
    section("S1 · the console needs a token through the tunnel")
    try:
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
    except ImportError:
        check("fastapi is installed (run with the project .venv)", False)
        return
    from src.main import app

    with TestClient(app) as remote:
        check("/health is open", remote.get("/health").status_code == 200)
        check("the console API without a token is 401", remote.get("/api/console/overview").status_code == 401)
        check("the console page without a token is 401", remote.get("/").status_code == 401)
        check("a wrong or odd-length token is 401, not 500",
              all(remote.get(f"/?token={t}").status_code == 401 for t in ("x", "check-token-longer", "")))
        check("the header token opens it", remote.get(
            "/api/console/overview", headers={"X-Console-Token": "check-token"}).status_code == 200)
        opened = remote.get("/?token=check-token")
        check("?token opens it and sets a cookie",
              opened.status_code == 200 and "console_token=" in opened.headers.get("set-cookie", ""))
        check("the cookie carries later requests", remote.get("/api/console/overview").status_code == 200)
        with remote.websocket_connect("/api/console/stream") as feed:
            check("the live feed accepts the cookie", feed.receive_json().get("type") == "hello")

    with TestClient(app) as fresh:
        try:
            with fresh.websocket_connect("/api/console/stream") as feed:
                feed.receive_json()
            check("the live feed without a token is refused", False)
        except WebSocketDisconnect as exc:
            check("the live feed without a token is refused", exc.code == 1008)
        with fresh.websocket_connect("/ws") as call:
            call.send_json({"event": "connected"})
        check("/ws, the platform's socket, needs no token", True)

    with TestClient(app, client=("127.0.0.1", 50000)) as local:
        check("this machine directly needs no token", local.get("/api/console/overview").status_code == 200)
        check("this machine through a proxy (ngrok) does",
              local.get("/api/console/overview", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 401)


async def run_async() -> None:
    await audio_checks()
    await silence_checks()
    await cache_checks()
    await tool_checks()


def main() -> int:
    asyncio.run(run_async())
    guard_checks()
    print(f"\n{'=' * 60}")
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All fix checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
