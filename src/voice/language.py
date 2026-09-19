"""Fast per-call language routing for the three challenge languages.

Deepgram's streaming ``multi`` model identifies English and Spanish but does
not include Catalan.  A small lexical signal catches Catalan without adding a
second prerecorded request to the first turn.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LanguageDecision:
    code: str
    confidence: float
    source: str


_CATALAN_PHRASES = (
    "bon dia",
    "bona tarda",
    "si us plau",
    "d acord",
    "moltes gracies",
)
# Distinctive only: "hora" and "necessito" are ordinary Spanish on a clinic line.
_CATALAN_WORDS = {
    "voldria", "demanar", "metge", "capcalera", "tingueu", "gracies",
    "aquesta", "aquest", "puc", "soc", "meva", "meu",
}
_ENGLISH_WORDS = {
    "hello", "appointment", "please", "thanks", "need", "doctor", "earliest",
    "morning", "afternoon", "insurance", "my", "yes",
}
_SPANISH_WORDS = {
    "hola", "cita", "gracias", "necesito", "doctor", "doctora", "temprano",
    "manana", "tarde", "seguro", "por", "favor",
}


def decide_language(
    text: str,
    deepgram_hint: Optional[str] = None,
    current: str = "es",
    established: bool = False,
) -> LanguageDecision:
    """Choose ``es``, ``ca`` or ``en`` without an extra network request."""
    clean = _normalise(text)
    words = set(re.findall(r"[a-z]+", clean))

    phrase_hits = sum(1 for phrase in _CATALAN_PHRASES if phrase in clean)
    catalan_hits = len(words & _CATALAN_WORDS) + phrase_hits * 2
    if catalan_hits >= 2:
        return LanguageDecision("ca", min(0.98, 0.72 + catalan_hits * 0.06), "catalan_markers")

    english_hits = len(words & _ENGLISH_WORDS)
    spanish_hits = len(words & _SPANISH_WORDS)
    # A lone "Hello?" is how many English callers start. It is not a line check,
    # and Deepgram often has no hint yet.
    if words and words <= {"hello", "hi", "hey"} and catalan_hits == 0 and spanish_hits == 0:
        return LanguageDecision("en", 0.78, "text_markers")
    if words and words <= {"hola"} and catalan_hits == 0 and english_hits == 0:
        return LanguageDecision("es", 0.78, "text_markers")
    if english_hits >= 2 and english_hits > spanish_hits:
        return LanguageDecision("en", min(0.92, 0.68 + english_hits * 0.05), "text_markers")
    if spanish_hits >= 2 and spanish_hits >= english_hits:
        return LanguageDecision("es", min(0.92, 0.68 + spanish_hits * 0.05), "text_markers")
    hint = canonical_language(deepgram_hint) or ""
    # Once the stream is locked to Catalan its hint naturally remains ``ca``;
    # strong text evidence is what lets a caller switch back mid-call.
    if hint == "ca" and english_hits >= 2 and english_hits > spanish_hits:
        return LanguageDecision("en", min(0.92, 0.68 + english_hits * 0.05), "text_markers")
    if hint == "ca" and spanish_hits >= 2 and spanish_hits >= english_hits:
        return LanguageDecision("es", min(0.92, 0.68 + spanish_hits * 0.05), "text_markers")
    if hint in {"es", "en", "ca"}:
        # A Spanish name ("José Martínez") is not a language switch once the
        # caller has already been speaking English — or the other way around.
        if (
            established
            and current in {"es", "en", "ca"}
            and current != hint
        ):
            if current == "en" and spanish_hits == 0 and catalan_hits == 0:
                return LanguageDecision(current, 0.55, "current")
            if current == "es" and english_hits == 0 and catalan_hits == 0:
                return LanguageDecision(current, 0.55, "current")
            if current == "ca" and english_hits == 0 and spanish_hits == 0:
                return LanguageDecision(current, 0.55, "current")
        # The streaming API exposes a language label but no language-specific
        # confidence.  The conservative value makes a strong Catalan lexical
        # decision able to override it.
        return LanguageDecision(hint, 0.80, "deepgram_stream")

    return LanguageDecision(current if current in {"es", "ca", "en"} else "es", 0.50, "current")


def should_apply_language(
    current: str,
    established: bool,
    decision: LanguageDecision,
) -> bool:
    """Use provider hints to establish a call, never to flip one on a name.

    Streaming recognisers often label a Spanish patient or doctor name as
    Spanish inside an otherwise English sentence. Once the call language is
    established, only lexical evidence may change it.
    """
    if decision.code == current:
        return True
    if not established:
        return decision.source != "current" and decision.confidence >= 0.72
    # Names are not a language switch. Only a real phrase in the new language is.
    return (
        decision.source in {"text_markers", "catalan_markers"}
        and decision.confidence >= 0.78
    )


def _normalise(value: str) -> str:
    folded = unicodedata.normalize("NFKD", (value or "").lower())
    return " ".join("".join(ch for ch in folded if not unicodedata.combining(ch)).split())


def canonical_language(code: Optional[str]) -> Optional[str]:
    """Map Scribe/Deepgram labels onto the three challenge languages."""
    if not code:
        return None
    raw = str(code).strip().lower().replace("_", "-")
    if not raw:
        return None
    short = raw.split("-")[0]
    return {
        "es": "es", "spa": "es", "spanish": "es",
        "en": "en", "eng": "en", "english": "en",
        "ca": "ca", "cat": "ca", "catalan": "ca",
    }.get(short)
