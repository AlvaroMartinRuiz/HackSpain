"""End-to-end checks of the scheduling engine against the real clinic.

No model is involved: this asserts that what the engine would submit is what
the API actually offered, and that a rule that bites is named correctly.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from v2.domain.catalog import Catalog  # noqa: E402
from v2.domain.engine import SchedulingEngine  # noqa: E402
from v2.domain.timeref import now_madrid, parse_slot  # noqa: E402
from v2.platform_api.client import PlatformClient  # noqa: E402

# The directory needs a given name plus a surname, or one exact field, so the
# sample is drawn by date of birth instead.
BIRTHDAYS = [
    "1988-03-14", "1975-06-02", "1992-11-21", "1965-01-30", "1980-09-09",
    "1955-04-18", "1998-07-07", "2015-05-12", "1970-12-25", "1983-08-08",
    "1948-10-10", "2001-03-03", "1961-05-19", "1995-09-27", "1972-02-14",
    "1958-11-03", "1990-06-20", "2011-01-17", "1968-07-24", "1979-04-05",
    "1986-12-11", "1953-03-28", "2004-08-16", "1996-10-02", "1963-09-13",
]

FAILURES: list[str] = []
_pool: list[dict[str, Any]] = []
_queried: set[str] = set()


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def _more_birthdays() -> list[str]:
    """Extra dates to widen the sample when a rarer plan is needed."""
    extra: list[str] = []
    for year in range(1945, 2020, 3):
        for month, day in ((3, 7), (7, 22), (11, 9)):
            extra.append(f"{year}-{month:02d}-{day:02d}")
    return extra


async def find_patient(
    client: PlatformClient,
    insurer: Optional[str] = None,
    seen_before: Optional[bool] = None,
    without_referral: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Pull real patients a date of birth at a time until one fits."""

    def fits(match: dict[str, Any]) -> bool:
        if insurer and match.get("insurer") != insurer:
            return False
        if seen_before is not None and match.get("has_visited_before") is not seen_before:
            return False
        if without_referral and without_referral in (match.get("referrals") or []):
            return False
        return True

    for match in _pool:
        if fits(match):
            return match

    seen = {m["patient_id"] for m in _pool}
    for birthday in BIRTHDAYS + _more_birthdays():
        if birthday in _queried:
            continue
        _queried.add(birthday)
        for match in await client.directory(date_of_birth=birthday):
            if match["patient_id"] in seen:
                continue
            seen.add(match["patient_id"])
            _pool.append(match)
            if fits(match):
                return match
    return None


async def main() -> int:
    catalog = Catalog.load()
    client = PlatformClient()
    engine = SchedulingEngine(client, catalog)
    now = now_madrid()
    print(f"Now in Madrid: {now.strftime('%A %Y-%m-%d %H:%M')}\n")

    try:
        print("The caller id is already a lookup")
        seed = await find_patient(client, seen_before=True)
        if seed is None:
            check("found a patient to work with", False)
            return 1
        by_phone = await engine.identify(phone=f"+34{seed['phone']}")
        check(
            "phone finds the chart",
            any(m["patient_id"] == seed["patient_id"] for m in by_phone["matches"]),
            f"{seed['patient_id']} via +34{seed['phone']}",
        )

        print("\nA surname on its own is a question, not an error")
        partial = await engine.identify(name=seed["first_surname"])
        check("the engine asks for more rather than failing",
              partial.get("needs_more") is True, str(partial["trace"][:1]))

        print("\nA misheard national id falls back instead of denying the patient exists")
        broken = seed["national_id"][:-1] + ("A" if seed["national_id"][-1] != "A" else "B")
        recovered = await engine.identify(
            name=f"{seed['given_name']} {seed['first_surname']}",
            national_id=broken,
            date_of_birth=seed["date_of_birth"],
        )
        check(
            "still found on name and date of birth",
            recovered["count"] >= 1,
            f"trace: {recovered['trace'][:1]}",
        )

        print("\nThe soonest general practice appointment")
        search = await engine.find_slots(
            patient_id=seed["patient_id"], specialty_id="general_practice", when="lo antes posible"
        )
        check("slots came back", search.found, f"{len(search.slots)} offered, reason={search.reason}")
        if search.found:
            first = search.slots[0]
            check("nothing on the day of the call", first.start.date() > now.date(),
                  first.start.isoformat())
            check("the type came from availability",
                  first.appointment_type_id == search.appointment_type_id,
                  str(search.appointment_type_id))
            expected = catalog.expected_appointment_type(
                "general_practice", bool(seed.get("has_visited_before"))
            )
            check("and it matches the record's own history",
                  expected is not None and expected.id == first.appointment_type_id,
                  f"{expected.id if expected else '?'} vs {first.appointment_type_id}")
            check("slots are ordered earliest first",
                  all(a.start <= b.start for a, b in zip(search.slots, search.slots[1:])))

            plan, reason = engine.plan_booking(seed, first)
            check("a payload was built", plan is not None, reason or "")
            if plan is not None:
                check("the plan billed is one the slot accepts",
                      plan.policy_id in first.payable_with,
                      f"{plan.policy_id} in {list(first.payable_with)}")
                check("the slot carries an explicit offset",
                      "+" in plan.slot or plan.slot.endswith("Z"), plan.slot)
                check("ids were not invented",
                      plan.provider_id in catalog.providers
                      and plan.location_id in catalog.locations)

        print("\nA doctor on leave: nothing during it, and the caller is told why")
        on_leave = await engine.find_slots(
            patient_id=seed["patient_id"], provider_id="PR02", when="mañana"
        )
        leave_end = catalog.providers["PR02"].leave_end
        check("nothing offered inside his leave",
              all(slot.start.date() > leave_end for slot in on_leave.slots),
              ", ".join(slot.start.strftime("%d %b") for slot in on_leave.slots[:3]) or "none")
        check("the leave is surfaced rather than silently worked around",
              any("leave" in note or "not back" in note for note in on_leave.notes),
              next(iter(on_leave.notes), str(on_leave.reason)))

        print("\nA day the clinic is shut rolls to the next open one")
        sunday = await engine.find_slots(
            patient_id=seed["patient_id"], specialty_id="general_practice", when="el domingo"
        )
        if sunday.found:
            check("no Sunday slot was offered",
                  all(slot.start.weekday() != 6 for slot in sunday.slots),
                  sunday.slots[0].start.strftime("%A %d %b"))
        else:
            check("no Sunday slot was offered", True, f"reason={sunday.reason}")

        print("\nAn afternoon ask no diary can meet is offered as a choice, not decided for them")
        # No general practitioner works a Thursday afternoon, so this is the
        # case where both near misses have to reach the caller.
        afternoon = await engine.find_slots(
            patient_id=seed["patient_id"], specialty_id="general_practice",
            when="el jueves por la tarde",
        )
        check("options came back", afternoon.found, f"{len(afternoon.slots)} offered")
        if afternoon.found:
            check("the shortfall is recorded",
                  any("nothing on" in line for line in afternoon.trace),
                  next((line for line in afternoon.trace if "nothing on" in line), ""))
            # General practice runs mornings only, so an honest answer says so
            # rather than implying an afternoon might turn up.
            check("the agent is told the afternoon does not exist here",
                  afternoon.part_of_day_possible is False,
                  ", ".join(slot.start.strftime("%a %H:%M") for slot in afternoon.slots[:4]))
            check("and it is said in words, not just a flag",
                  any("afternoon" in note for note in afternoon.notes),
                  next(iter(afternoon.notes), ""))

        print("\nAn afternoon ask a diary can meet is answered in the afternoon")
        derm = await engine.find_slots(
            patient_id=seed["patient_id"], specialty_id="orthopaedics", when="por la tarde",
        )
        if derm.found:
            check("first option is after 14:00", derm.slots[0].start.hour >= 14,
                  derm.slots[0].start.strftime("%a %H:%M"))
        else:
            check("orthopaedics answered", derm.reason is not None, str(derm.reason))

        print("\nA plan that does not cover the specialty is a refusal, not a booking")
        adeslas = await find_patient(client, insurer="adeslas")
        if adeslas is None:
            check("found an Adeslas patient", False)
        else:
            gynae = await engine.find_slots(
                patient_id=adeslas["patient_id"], specialty_id="gynaecology", when="lo antes posible"
            )
            check("Adeslas gets no gynaecology", not gynae.found,
                  f"{len(gynae.slots)} slots, blocked={gynae.blocked}")
            check("and the reason is the rule that bit",
                  gynae.reason in {"specialty_not_covered", "no_availability"},
                  str(gynae.reason))
            pre_check = engine.check_eligibility(adeslas, "gynaecology", now)
            check("the rule is visible before the calendar is opened",
                  pre_check == "specialty_not_covered", str(pre_check))

        print("\nASISA can never book physiotherapy at all")
        asisa = await find_patient(client, insurer="asisa")
        if asisa is None:
            check("found an ASISA patient", False)
        else:
            physio = await engine.find_slots(
                patient_id=asisa["patient_id"], specialty_id="physiotherapy", when="lo antes posible"
            )
            check("no physiotherapy for ASISA", not physio.found,
                  f"reason={physio.reason}, blocked={physio.blocked}")

        print("\nA referral-only specialty without the referral")
        no_referral = await find_patient(client, without_referral="dermatology")
        if no_referral:
            derm = engine.check_eligibility(no_referral, "dermatology", now)
            check("dermatology needs a referral", derm == "referral_required", str(derm))

        print("\nThe nearest site that can actually serve the request")
        physio_from_centre = engine.nearest_site("Gran Vía 1, Madrid", "physiotherapy")
        check("the only physiotherapist is at Sur, so Sur it is",
              (physio_from_centre.get("nearest_serving") or {}).get("location_id") == "sur",
              str((physio_from_centre.get("nearest_serving") or {}).get("location_id")))
        gp_from_getafe = engine.nearest_site("Calle de Madrid 54, Getafe", "general_practice")
        check("Getafe's nearest GP site is Sur",
              (gp_from_getafe.get("nearest_serving") or {}).get("location_id") == "sur",
              str((gp_from_getafe.get("nearest_serving") or {}).get("location_id")))

        print("\nAn appointment id only ever comes from the diary")
        context = await engine.patient_context(seed["patient_id"])
        check("the chart was read", "upcoming" in context,
              f"{context['visit_count']} past visits, {len(context['upcoming'])} upcoming")
        check("every upcoming appointment has an id to act on",
              all(a.get("appointment_id") for a in context["upcoming"]))
        # The API's own split between upcoming and past is the authority, not our
        # clock: a few entries it calls upcoming already sit behind today.
        stale = [a for a in context["upcoming"] if parse_slot(a["slot"]) < now]
        if stale:
            print(f"        note: {len(stale)} entry/entries the API calls upcoming are already "
                  f"behind us ({stale[0]['when']}) — its classification is what we act on")

    finally:
        await client.aclose()

    print(f"\n{'=' * 60}")
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All engine checks passed against the live clinic.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
