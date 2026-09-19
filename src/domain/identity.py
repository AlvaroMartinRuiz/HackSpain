"""Names, national ids, phones and emails as they arrive over the phone."""

from __future__ import annotations

import re
import unicodedata

DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
NIE_PREFIX = {"X": "0", "Y": "1", "Z": "2"}

TITLES = ("dra.", "dra", "dr.", "dr", "doctora", "doctor", "d.", "dna.", "doña", "don")

_SPOKEN_DIGITS = {
    "cero": "0", "zero": "0", "uno": "1", "una": "1", "one": "1", "dos": "2", "two": "2",
    "tres": "3", "three": "3", "cuatro": "4", "four": "4", "cinco": "5", "five": "5",
    "seis": "6", "six": "6", "siete": "7", "seven": "7", "ocho": "8", "eight": "8",
    "nueve": "9", "nine": "9",
}

_EMAIL_WORDS = [
    (r"\b(arroba|at sign|at)\b", "@"),
    (r"\b(punto|dot|period)\b", "."),
    (r"\b(gui[oó]n bajo|underscore|under score)\b", "_"),
    (r"\b(gui[oó]n|dash|hyphen)\b", "-"),
]


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize_text(text: str) -> str:
    """Lowercase, unaccented, single-spaced — for comparing what was said."""
    return re.sub(r"\s+", " ", strip_accents(text or "").lower()).strip()


def normalize_provider_name(name: str) -> str:
    cleaned = normalize_text(name)
    for title in TITLES:
        if cleaned.startswith(title + " "):
            cleaned = cleaned[len(title) + 1 :]
            break
    return cleaned.strip()


def normalize_phone(raw: str) -> str:
    """Fold to the nine national digits, the way the directory compares them."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0034"):
        digits = digits[4:]
    elif digits.startswith("34") and len(digits) > 9:
        digits = digits[2:]
    return digits[-9:] if len(digits) >= 9 else digits


def spoken_to_digits(text: str) -> str:
    """"cuatro uno dos" -> "412"; digits already present are kept."""
    out: list[str] = []
    for token in re.findall(r"[a-záéíóúñ]+|\d", normalize_text(text)):
        if token.isdigit():
            out.append(token)
        elif token in _SPOKEN_DIGITS:
            out.append(_SPOKEN_DIGITS[token])
    return "".join(out)


def dni_check_letter(digits: str) -> str:
    return DNI_LETTERS[int(digits) % 23]


def nie_check_letter(body: str) -> str:
    """``body`` is the prefix letter plus seven digits."""
    prefix, digits = body[0].upper(), body[1:]
    return DNI_LETTERS[int(NIE_PREFIX[prefix] + digits) % 23]


def parse_national_id(raw: str) -> dict[str, object]:
    """Read a DNI or NIE as dictated and re-derive its own check letter.

    The letter is what separates a misheard digit from an invented one, so the
    expected letter is always returned alongside whatever was heard.
    """
    cleaned = re.sub(r"[^0-9A-Za-z]", "", raw or "").upper()
    result: dict[str, object] = {"input": raw, "cleaned": cleaned, "kind": None,
                                 "valid": False, "value": None, "expected_letter": None}
    if not cleaned:
        return result

    if cleaned[0] in NIE_PREFIX:
        body, letter = cleaned[:8], cleaned[8:9]
        if len(body) == 8 and body[1:].isdigit():
            expected = nie_check_letter(body)
            result.update(kind="NIE", expected_letter=expected, value=body + expected,
                          valid=letter == expected)
        return result

    digits = cleaned[:8]
    letter = cleaned[8:9]
    if len(digits) == 8 and digits.isdigit():
        expected = dni_check_letter(digits)
        result.update(kind="DNI", expected_letter=expected, value=digits + expected,
                      valid=letter == expected)
    return result


def normalize_email(raw: str) -> str:
    """Turn a dictated address into one: "ana punto garcia arroba gmail punto com"."""
    text = normalize_text(raw)
    for pattern, replacement in _EMAIL_WORDS:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\s*([@._-])\s*", r"\1", text)
    return text.replace(" ", "")


def name_tokens(*parts: str) -> list[str]:
    tokens: list[str] = []
    for part in parts:
        tokens.extend(token for token in normalize_text(part).split(" ") if token)
    return tokens
