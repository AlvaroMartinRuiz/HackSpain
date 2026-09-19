"""Self-checks for the deterministic core.

Nothing here talks to a model. These are the parts that decide what ends up in
a record, so they are asserted rather than trusted.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from src.domain.catalog import Catalog, age_months  # noqa: E402
from src.domain.engine import SchedulingEngine  # noqa: E402
from src.domain.gazetteer import locate  # noqa: E402
from src.domain.identity import (  # noqa: E402
    normalize_email,
    normalize_phone,
    parse_national_id,
)
from src.domain.outcomes import pick_blocking_reason  # noqa: E402
from src.domain.timeref import MADRID, resolve_when  # noqa: E402
from src.domain.triage import triage  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got: object, want: object) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got!r}")
        print(f"        want {want!r}")
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


def main() -> int:
    catalog = Catalog.load()

    section("National ids — the check letter is what separates misheard from invented")
    check("valid DNI", parse_national_id("12345678Z")["valid"], True)
    check("wrong letter is caught", parse_national_id("12345678A")["valid"], False)
    check("expected letter derived", parse_national_id("12345678A")["expected_letter"], "Z")
    check("NIE with prefix", parse_national_id("X1234567L")["valid"], True)
    check("spaces and dashes ignored", parse_national_id("12.345.678-Z")["value"], "12345678Z")

    section("Phones fold to nine digits, whatever arrives")
    for raw in ("+34612345678", "0034612345678", "612 345 678", "612345678"):
        check(f"{raw!r}", normalize_phone(raw), "612345678")

    section("Dictated emails")
    check(
        "spoken form",
        normalize_email("ana punto garcia arroba gmail punto com"),
        "ana.garcia@gmail.com",
    )
    check("already written", normalize_email("Ana.Garcia@Gmail.com"), "ana.garcia@gmail.com")
    check(
        "underscore",
        normalize_email("ana guion bajo garcia arroba hotmail punto es"),
        "ana_garcia@hotmail.es",
    )

    section("Insurance names survive ordinary speech-recognition errors")
    engine = SchedulingEngine(None, catalog)  # type: ignore[arg-type]
    check("Sinitas resolves to Sanitas", engine.resolve_insurer("Sinitas"), "sanitas")
    check("Nueva Mutua keeps its separator", engine.resolve_insurer("Nueva Mutua"), "nueva_mutua")

    section("Appointment type follows the specialty and the record, never the request")
    cases = [
        ("general_practice", True, "review"),
        ("general_practice", False, "first_visit"),
        ("dermatology", True, "dermatology_review"),
        ("dermatology", False, "dermatology_first_visit"),
        ("orthopaedics", True, "orthopaedic_review"),
        ("paediatrics", True, "paediatric_review"),
        ("physiotherapy", False, "physiotherapy_assessment"),
        # Gynaecology has a review of its own but no first visit, so a new
        # patient falls back to the universal one.
        ("gynaecology", True, "gynaecology_review"),
        ("gynaecology", False, "first_visit"),
    ]
    for specialty, seen_before, expected in cases:
        got = catalog.expected_appointment_type(specialty, seen_before)
        check(f"{specialty} / seen={seen_before}", got.id if got else None, expected)

    section("Names a phone line cannot separate come back as two")
    for spoken, expected in [
        ("doctor Sáez", ["Dr. Martín Sáez", "Dra. Marta Sáenz"]),
        ("doctora Sáenz", ["Dra. Marta Sáenz", "Dr. Martín Sáez"]),
        ("doctora Iglesias", ["Dra. Elena Iglesias", "Dr. Emilio Iglesia"]),
        ("doctora Montoro", ["Dra. Isabel Montoro"]),
        ("Pepito Grillo", []),
    ]:
        check(spoken, [p.name for p in catalog.find_providers_by_name(spoken)], expected)

    section("The 14th birthday is the boundary, in months, with no gap")
    paediatrics = catalog.specialties["paediatrics"]
    general = catalog.specialties["general_practice"]
    check("13y11m is paediatric", paediatrics.accepts_age_months(167), True)
    check("13y11m is not GP", general.accepts_age_months(167), False)
    check("14y0m is GP", general.accepts_age_months(168), True)
    check("14y0m is not paediatric", paediatrics.accepts_age_months(168), False)
    check(
        "age in months",
        age_months(date(2012, 9, 19), date(2026, 9, 19)),
        168,
    )

    section("Relative dates, resolved against the moment the call connects")
    # A Saturday, so a weekday phrase for Saturday lands a week out.
    now = datetime(2026, 9, 19, 10, 30, tzinfo=MADRID)
    expectations = [
        ("mañana", date(2026, 9, 20), None),
        ("pasado mañana", date(2026, 9, 21), None),
        ("el jueves que viene", date(2026, 9, 24), None),
        ("this coming Thursday", date(2026, 9, 24), None),
        ("este sábado", date(2026, 9, 26), None),
        ("on Saturday morning", date(2026, 9, 26), "morning"),
        ("el viernes por la tarde", date(2026, 9, 25), "afternoon"),
        ("in a fortnight", date(2026, 10, 3), None),
        ("dentro de una semana", date(2026, 9, 26), None),
        ("first thing on Monday the twelfth of October", date(2026, 10, 12), "morning"),
        ("por la mañana", None, "morning"),
    ]
    for phrase, expected_date, expected_part in expectations:
        spec = resolve_when(phrase, now)
        check(f"{phrase!r} date", spec.target_date, expected_date)
        if expected_part is not None:
            check(f"{phrase!r} part", spec.part_of_day, expected_part)

    check("'lo antes posible' asks for the soonest", resolve_when("lo antes posible", now).soonest, True)

    section("Closures")
    check("Sunday is shut everywhere", catalog.is_open(date(2026, 9, 20)), False)
    check("Saturday is Centro only", catalog.is_open(date(2026, 9, 26), "norte"), False)
    check("Saturday at Centro", catalog.is_open(date(2026, 9, 26), "centro"), True)
    check("Fiesta Nacional", catalog.is_open(date(2026, 10, 12)), False)
    check(
        "next open day after the closure",
        catalog.next_open_day(date(2026, 10, 12)),
        date(2026, 10, 13),
    )

    section("Nearest site, by straight-line distance")
    for where, expected in [
        ("Calle de Madrid 54, en Getafe", "sur"),
        ("Alberto Alcocer, Chamartín", "norte"),
        ("Gran Vía, Madrid centro", "centro"),
        ("estoy en Alcobendas", "norte"),
        ("vivo en Leganés", "sur"),
    ]:
        located = locate(where)
        if located is None:
            check(where, None, expected)
            continue
        _place, latitude, longitude = located
        ranked = catalog.rank_locations_by_distance(latitude, longitude)
        check(where, ranked[0][0].id, expected)

    section("ASISA can never book physiotherapy: it is only at Sur, which ASISA does not cover")
    physio_sites = catalog.locations_serving("physiotherapy")
    check("only site with a physiotherapist", sorted(physio_sites), ["sur"])
    check("ASISA does not cover Sur", catalog.plan_covers_location("asisa", "sur"), False)
    check("Adeslas does not cover gynaecology", catalog.plan_covers_specialty("adeslas", "gynaecology"), False)
    check("Dra. Iglesias refuses DKV", "dkv" in catalog.providers["PR05"].refused_insurers, True)
    check("Dr. Vilar takes DKV", "dkv" in catalog.providers["PR12"].refused_insurers, False)

    section("Dr. Requena is on leave for the whole event")
    requena = catalog.providers["PR02"]
    check("on leave on the 19th", requena.on_leave_on(date(2026, 9, 19)), True)
    check("back on the 1st of October", requena.on_leave_on(date(2026, 10, 1)), False)

    section("Triage: red flags first, then the route")
    check(
        "chest pain escalates",
        triage("tight pain across the chest and struggling to catch my breath").red_flag is not None,
        True,
    )
    check(
        "stroke escalates",
        triage("one side of her face has gone droopy and her arm is weak, words slurred").red_flag is not None,
        True,
    )
    check("ankle goes to orthopaedics", triage("I went over on my ankle, it is swollen").specialty_id, "orthopaedics")
    check(
        "child with a fever goes to paediatrics",
        triage("my child has had a temperature for two days and is off their food").specialty_id,
        "paediatrics",
    )
    check("headaches go to GP", triage("headaches most afternoons for a month").specialty_id, "general_practice")
    check(
        "heavy periods go to gynaecology",
        triage("very heavy, irregular periods for months").specialty_id,
        "gynaecology",
    )

    section("A blocked availability answer names the rule to report")
    check(
        "a patient rule outranks one provider's leave",
        pick_blocking_reason([
            {"provider_id": "PR02", "restriction": "provider_on_leave"},
            {"provider_id": "PR05", "restriction": "specialty_not_covered"},
        ]),
        "specialty_not_covered",
    )
    check("empty blocked means the calendar is simply full", pick_blocking_reason([]), None)

    print(f"\n{'=' * 60}")
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All domain checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
