"""Checks for product views derived from call events. No network, no model."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from src.notify.email import compose, format_when, ics_for, maps_url
from src.obs.demo_story import story
from src.obs.product import derive_journey, hearing_support_from_note, overlay

FAILURES: list[str] = []


def check(label: str, ok: object, detail: object = None) -> None:
    passed = bool(ok)
    print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    if not passed:
        if detail is not None:
            print(f"        {detail!r}")
        FAILURES.append(label)


def main() -> int:
    sample = story()
    product = sample["product"]

    print("\nPatient journey")
    ids = [step["id"] for step in product["journey"]]
    check("starts connected", ids[0] == "connected")
    check("language is on the journey", any(i.startswith("language:") for i in ids))
    check("patient identified", "patient" in ids)
    check("chart opened", "chart" in ids)
    check("interruption recorded", "interrupt" in ids)
    check("second search after a change of mind", any(i.startswith("search:") for i in ids) or "changed" in ids)
    check("book accepted", "submit:book" in ids)
    check("follow-up on the journey", "followup" in ids)

    print("\nIntents")
    intents = product["intents"]
    check("one book intent", len(intents) == 1 and intents[0]["id"] == "book")
    check("intent is done", intents[0]["state"] == "done")

    print("\nWhy this decision")
    why = product["why"]
    labels = {row["label"] for row in why["facts"]}
    check("has insurance", "Insurance" in labels)
    check("has a constraint", "Constraint" in labels)
    check("does not invent a reference", "reference" not in (why.get("decision") or "").lower())
    check("empty why is honest", overlay()["why"]["headline"].startswith("No additional"))

    print("\nSafety + context")
    kinds = {card["kind"] for card in product["safety"]}
    check("insurance shield visible", "insurance" in kinds)
    ctx = product["patient_context"]
    check("regular patient", ctx["regular"] is True and ctx["visit_count"] == 11)
    check("hearing support from the note", ctx["hearing_support"] is True)
    check("note matcher is conservative", not hearing_support_from_note("likes mornings"))

    print("\nResolution")
    res = product["resolution"]
    check("book outcome", res["outcomes"][0]["action"] == "book")
    check("language path shown", "Spanish" in res["language"] and "Catalan" in res["language"])
    check("interruptions counted", res["interruptions"] == 1)

    print("\nFollow-up email (git)")
    message = compose(
        "book",
        language="es",
        patient_name="Elena García",
        details={"when": "martes 10:30", "doctor": "Dr. Vilar", "site": "Arenal Norte"},
    )
    check("spanish subject", "Clínica Arenal" in message["subject"])
    check("html names the doctor", "Vilar" in message["html"])
    check("no invented reference", "referencia" not in message["html"].lower() and "reference" not in message["text"].lower())
    check("format_when keeps a slot", bool(format_when("2026-09-22T10:30:00+02:00", "es")))

    booked = compose(
        "book",
        language="es",
        patient_name="Elena García",
        details={
            "when": "martes 10:30",
            "doctor": "Dr. Vilar",
            "site": "Arenal Norte",
            "address": "Arenal Norte, Madrid",
            "slot_iso": "2026-09-22T10:30:00+02:00",
        },
    )
    check("ics is a VEVENT", "BEGIN:VEVENT" in (booked.get("ics") or ""))
    check("maps points at the site", "maps" in (booked.get("maps_url") or ""))
    check("directions in the html", "Cómo llegar" in booked["html"])
    check("cancel has no ics", not ics_for(kind="cancel", slot="2026-09-22T10:30:00+02:00"))
    check("maps helper", "google.com/maps" in maps_url("Arenal Norte, Madrid"))

    print("\nJourney is not a static checklist")
    empty = derive_journey([], [], [], [], "es")
    check("quiet call is only connected + language", len(empty) <= 2)

    if FAILURES:
        print(f"\n{len(FAILURES)} product check(s) failed")
        return 1
    print("\nAll product checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
