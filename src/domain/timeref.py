"""Resolving what the caller meant by "next Thursday", in Europe/Madrid.

Dates resolve against the moment the call connects, never against a fixed
anchor or the machine clock, and nothing is ever booked for the same day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from src.domain.identity import normalize_text

MADRID = ZoneInfo("Europe/Madrid")

MORNING = "morning"
AFTERNOON = "afternoon"
AFTERNOON_FROM = time(14, 0)

WEEKDAYS: dict[str, int] = {
    "monday": 0, "lunes": 0, "dilluns": 0,
    "tuesday": 1, "martes": 1, "dimarts": 1,
    "wednesday": 2, "miercoles": 2, "dimecres": 2,
    "thursday": 3, "jueves": 3, "dijous": 3,
    "friday": 4, "viernes": 4, "divendres": 4,
    "saturday": 5, "sabado": 5, "dissabte": 5,
    "sunday": 6, "domingo": 6, "diumenge": 6,
}

MONTHS: dict[str, int] = {
    "january": 1, "enero": 1, "gener": 1,
    "february": 2, "febrero": 2, "febrer": 2,
    "march": 3, "marzo": 3, "marc": 3,
    "april": 4, "abril": 4,
    "may": 5, "mayo": 5, "maig": 5,
    "june": 6, "junio": 6, "juny": 6,
    "july": 7, "julio": 7, "juliol": 7,
    "august": 8, "agosto": 8, "agost": 8,
    "september": 9, "septiembre": 9, "setembre": 9,
    "october": 10, "octubre": 10,
    "november": 11, "noviembre": 11, "novembre": 11,
    "december": 12, "diciembre": 12, "desembre": 12,
}

ORDINALS: dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "twenty-first": 21, "twenty-second": 22, "twenty-third": 23,
    "twenty-fourth": 24, "twenty-fifth": 25, "twenty-sixth": 26,
    "twenty-seventh": 27, "twenty-eighth": 28, "twenty-ninth": 29,
    "thirtieth": 30, "thirty-first": 31,
}

_FIRST_THING = r"(first thing|a primera hora|primera hora|lo m[aá]s temprano)"
_MORNING = r"(morning|ma[nñ]ana|mat[ií]|por la ma[nñ]ana|de la ma[nñ]ana)"
_AFTERNOON = r"(afternoon|tarde|por la tarde|de la tarde|"\
             r"from two o'?clock|from 2 o'?clock|after two o'?clock|"\
             r"a partir de las dos|desde las dos)"
_SOONEST = r"(soonest|earliest|as soon as possible|asap|lo antes posible|cuanto antes|"\
           r"lo m[aá]s pronto|la m[aá]s pr[oó]xima|el primer hueco|primer hueco)"


@dataclass
class WhenSpec:
    """A caller's time ask, resolved to something the calendar can answer."""

    raw: str
    target_date: Optional[date] = None
    part_of_day: Optional[str] = None
    first_thing: bool = False
    soonest: bool = False
    matched: Optional[str] = None

    def describe(self) -> str:
        bits = []
        if self.target_date:
            bits.append(self.target_date.isoformat())
        if self.part_of_day:
            bits.append(self.part_of_day)
        if self.first_thing:
            bits.append("first thing")
        if self.soonest:
            bits.append("soonest")
        return ", ".join(bits) or "no constraint"


def now_madrid() -> datetime:
    return datetime.now(MADRID)


def resolve_when(phrase: Optional[str], now: Optional[datetime] = None) -> WhenSpec:
    """Turn a spoken time phrase into a date, a part of the day, or neither."""
    now = now or now_madrid()
    today = now.date()
    text = normalize_text(phrase or "")
    spec = WhenSpec(raw=phrase or "")

    if not text:
        spec.soonest = True
        spec.matched = "empty"
        return spec

    if re.search(_FIRST_THING, text):
        spec.first_thing = True
        spec.part_of_day = MORNING
        # "first thing on Monday the twelfth" must not read "first" as a day.
        text = re.sub(_FIRST_THING, " ", text).strip()

    if re.search(_AFTERNOON, text):
        spec.part_of_day = AFTERNOON
    elif re.search(r"por la ma[nñ]ana|de la ma[nñ]ana|in the morning|\bmorning\b|\bmat[ií]\b", text):
        spec.part_of_day = MORNING

    if re.search(_SOONEST, text):
        spec.soonest = True
        spec.matched = "soonest"

    explicit = _explicit_date(text, today)
    if explicit is not None:
        spec.target_date = explicit
        spec.matched = spec.matched or "explicit_date"
        return spec

    if re.search(r"\b(day after tomorrow|pasado ma[nñ]ana|dem[aà] passat)\b", text):
        spec.target_date = today + timedelta(days=2)
        spec.matched = "day_after_tomorrow"
        return spec

    # "mañana" is tomorrow unless it was the part of the day ("por la mañana").
    if re.search(r"\btomorrow\b", text) or (
        re.search(r"\bma[nñ]ana\b", text) and not re.search(r"(por|de) la ma[nñ]ana", text)
    ):
        spec.target_date = today + timedelta(days=1)
        spec.matched = "tomorrow"
        return spec

    if re.search(r"(a week from today|in a week|en una semana|dentro de una semana|"
                 r"la semana que viene|next week|la pr[oó]xima semana)", text):
        spec.target_date = today + timedelta(days=7)
        spec.matched = "in_a_week"
        return spec

    if re.search(r"(fortnight|in two weeks|en dos semanas|dentro de dos semanas|"
                 r"quince d[ií]as|en 15 d[ií]as)", text):
        spec.target_date = today + timedelta(days=14)
        spec.matched = "fortnight"
        return spec

    weekday = _weekday_in(text)
    if weekday is not None:
        spec.target_date = next_weekday(today, weekday)
        spec.matched = "weekday"
        return spec

    if spec.matched is None:
        spec.soonest = True
        spec.matched = "unparsed"
    return spec


def next_weekday(today: date, weekday: int) -> date:
    """The first such weekday strictly after today, the way a caller means it."""
    ahead = (weekday - today.weekday()) % 7
    return today + timedelta(days=ahead or 7)


def _weekday_in(text: str) -> Optional[int]:
    for token in re.findall(r"[a-z]+", text):
        if token in WEEKDAYS:
            return WEEKDAYS[token]
    return None


def _explicit_date(text: str, today: date) -> Optional[date]:
    month = None
    for token in re.findall(r"[a-z]+", text):
        if token in MONTHS:
            month = MONTHS[token]
            break
    if month is None:
        match = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", text)
        if match:
            day, month = int(match.group(1)), int(match.group(2))
            return _with_year(day, month, today)
        return None

    day = None
    numeric = re.search(r"\b(\d{1,2})\b", text)
    if numeric:
        day = int(numeric.group(1))
    else:
        # Longest first, so "twenty-first" is not read as "first".
        for word in sorted(ORDINALS, key=len, reverse=True):
            if re.search(rf"\b{re.escape(word)}\b", text):
                day = ORDINALS[word]
                break
    if day is None:
        return None
    return _with_year(day, month, today)


def _with_year(day: int, month: int, today: date) -> Optional[date]:
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate
    return None


def part_of_day_of(moment: datetime) -> str:
    return MORNING if moment.time() < AFTERNOON_FROM else AFTERNOON


def matches_part_of_day(moment: datetime, part: Optional[str]) -> bool:
    return part is None or part_of_day_of(moment) == part


def parse_slot(value: str) -> datetime:
    """Read an API timestamp and express it in Europe/Madrid."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MADRID)
    return moment.astimezone(MADRID)


def format_slot(moment: datetime) -> str:
    """The exact minute with an explicit offset, as a submission needs it."""
    return moment.astimezone(MADRID).replace(second=0, microsecond=0).isoformat()
