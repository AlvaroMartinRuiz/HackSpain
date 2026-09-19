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
_CATALAN_WORDS = {
    "voldria", "demanar", "metge", "capcalera", "tingueu", "gracies",
    "necessito", "hora", "aquesta", "aquest", "puc", "soc", "meva", "meu",
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
    hint = (deepgram_hint or "").lower()[:2]
    # Once the stream is locked to Catalan its hint naturally remains ``ca``;
    # strong text evidence is what lets a caller switch back mid-call.
    if hint == "ca" and english_hits >= 2 and english_hits > spanish_hits:
        return LanguageDecision("en", min(0.92, 0.68 + english_hits * 0.05), "text_markers")
    if hint == "ca" and spanish_hits >= 2 and spanish_hits >= english_hits:
        return LanguageDecision("es", min(0.92, 0.68 + spanish_hits * 0.05), "text_markers")
    if hint in {"es", "en", "ca"}:
        # The streaming API exposes a language label but no language-specific
        # confidence.  The conservative value makes a strong Catalan lexical
        # decision able to override it.
        return LanguageDecision(hint, 0.80, "deepgram_stream")

    if english_hits >= 2 and english_hits > spanish_hits:
        return LanguageDecision("en", min(0.92, 0.68 + english_hits * 0.05), "text_markers")
    if spanish_hits >= 2 and spanish_hits >= english_hits:
        return LanguageDecision("es", min(0.92, 0.68 + spanish_hits * 0.05), "text_markers")
    return LanguageDecision(current if current in {"es", "ca", "en"} else "es", 0.50, "current")


def _normalise(value: str) -> str:
    folded = unicodedata.normalize("NFKD", (value or "").lower())
    return " ".join("".join(ch for ch in folded if not unicodedata.combining(ch)).split())
