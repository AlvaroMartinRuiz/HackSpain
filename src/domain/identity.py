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

# NIE prefixes as they are dictated. The check letter is derived, so only the
# leading letter has to be identified as X, Y or Z.
_SPOKEN_NIE_PREFIX = (
    (r"^(i griega|ye)\b", "Y"),
    (r"^(equis)\b", "X"),
    (r"^(zeta|zeda)\b", "Z"),
)

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
    if len(digits) < 9:
        spoken = spoken_to_digits(raw)
        if len(spoken) > len(digits):
            digits = spoken
    if digits.startswith("0034"):
        digits = digits[4:]
    elif digits.startswith("34") and len(digits) > 9:
        digits = digits[2:]
    # A tenth leading 6 is usually STT repeating the mobile prefix.
    if len(digits) == 10 and digits.startswith("66"):
        return digits[:9]
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
    expected letter is always returned alongside whatever was heard. A letter
    that was simply never said is not the same as a wrong letter: the digits
    still determine it, and `letter_missing` says so.
    """
    cleaned = _clean_national_id(raw)
    result: dict[str, object] = {"input": raw, "cleaned": cleaned, "kind": None,
                                 "valid": False, "value": None, "expected_letter": None,
                                 "letter_missing": False}
    if not cleaned:
        return result

    if cleaned[0] in NIE_PREFIX:
        body, letter = cleaned[:8], cleaned[8:9]
        if len(body) == 8 and body[1:].isdigit():
            # A digit after the body is a glued phone, not a check letter.
            if letter.isdigit():
                return result
            expected = nie_check_letter(body)
            result.update(kind="NIE", expected_letter=expected, value=body + expected,
                          valid=letter == expected, letter_missing=not letter)
        return result

    digits = cleaned[:8]
    letter = cleaned[8:9]
    if len(digits) == 8 and digits.isdigit():
        if letter.isdigit():
            return result
        expected = dni_check_letter(digits)
        result.update(kind="DNI", expected_letter=expected, value=digits + expected,
                      valid=letter == expected, letter_missing=not letter)
    return result


def _clean_national_id(raw: str) -> str:
    """Alphanumeric form, falling back to digits and NIE prefixes as spoken."""
    cleaned = re.sub(r"[^0-9A-Za-z]", "", raw or "").upper()
    if _looks_like_id(cleaned):
        return cleaned
    spoken = _from_spoken_id(raw)
    return spoken if _looks_like_id(spoken) else cleaned


def _looks_like_id(cleaned: str) -> bool:
    if len(cleaned) >= 8 and cleaned[0] in NIE_PREFIX and cleaned[1:8].isdigit():
        return True
    return len(cleaned) >= 8 and cleaned[:8].isdigit()


def _from_spoken_id(raw: str) -> str:
    text = normalize_text(raw)
    prefix = ""
    for pattern, letter in _SPOKEN_NIE_PREFIX:
        if re.match(pattern, text):
            prefix = letter
            text = re.sub(pattern, "", text, count=1)
            break
    if not prefix and text[:1] in {"x", "y", "z"} and (len(text) == 1 or not text[1].isalpha()):
        prefix = text[0].upper()
        text = text[1:]
    digits = spoken_to_digits(text)
    if not digits:
        return ""
    return prefix + digits if prefix else digits


def split_id_and_phone(national_id: str, phone: str) -> tuple[str, str]:
    """Unstick a DNI/NIE and a mobile that STT concatenated into one string."""
    id_raw = (national_id or "").strip()
    phone_raw = (phone or "").strip()
    id_digits = re.sub(r"\D", "", id_raw)
    phone_digits = re.sub(r"\D", "", phone_raw)

    phone_is_separate = (
        len(normalize_phone(phone_raw)) == 9 and phone_digits != id_digits and len(id_digits) <= 9
    )
    if phone_is_separate:
        return id_raw, phone_raw

    blob = phone_raw if len(phone_digits) > len(id_digits) else id_raw
    cleaned = re.sub(r"[^0-9A-Za-z]", "", blob).upper()

    if cleaned[:1] in NIE_PREFIX and len(cleaned) >= 8 and cleaned[1:8].isdigit():
        nie = cleaned[:8]
        rest = cleaned[8:]
        if rest[:1].isalpha():
            nie += rest[:1]
            rest = rest[1:]
        rest_digits = re.sub(r"\D", "", rest)
        if len(rest_digits) >= 9:
            return nie, rest_digits[-9:]

    digits = re.sub(r"\D", "", cleaned)
    if len(digits) >= 16:
        mobile, body = digits[-9:], digits[:-9]
        if len(body) == 8:
            return body, mobile
        if len(body) == 7:
            # Prefix and letter dropped; X is the usual NIE prefix.
            return "X" + body, mobile
        if len(body) == 9 and body[0] in NIE_PREFIX:
            return body, mobile
    return id_raw, phone_raw


# STT often glues the insurer onto the domain: "outlook.escinitas", "gmail.comsinitas".
_EMAIL_INSURER_TAIL = re.compile(
    r"(?P<tld>com|es|net|org)\.?(?P<ins>sinitas|zinitas|cinitas|sanitas|adeslas|"
    r"asisa|dkv|axa|aegon|mapfre|privado|nuevamutua|nueva_?mutua)$",
    re.I,
)
_INSURER_FROM_STT = {
    "sinitas": "sanitas", "zinitas": "sanitas", "cinitas": "sanitas",
    "sanitas": "sanitas", "adeslas": "adeslas", "asisa": "asisa",
    "dkv": "dkv", "axa": "axa", "aegon": "aegon", "mapfre": "mapfre",
    "privado": "privado", "nuevamutua": "nueva_mutua", "nueva_mutua": "nueva_mutua",
}


def peel_insurer_from_email(email: str) -> tuple[str, str | None]:
    """Split an insurer name that STT appended to the domain, if one is there."""
    local, sep, domain = email.partition("@")
    if not sep:
        return email, None
    match = _EMAIL_INSURER_TAIL.search(domain)
    if not match:
        return email, None
    cleaned = f"{local}@{domain[:match.start()]}{match.group('tld').lower()}"
    spoken = _INSURER_FROM_STT.get(match.group("ins").lower())
    return cleaned, spoken


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
