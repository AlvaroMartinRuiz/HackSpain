"""Lines the agent says without asking the model, in each language it speaks.

They are fixed text, so the voice can be synthesised once and reused.
"""

from __future__ import annotations

SILENCE_PROMPTS: dict[str, list[str]] = {
    "en": ["Are you still there?",
           "Can you hear me? I'm still here whenever you're ready."],
    "es": ["¿Sigue ahí?",
           "¿Me oye? Sigo aquí cuando quiera."],
    "ca": ["Encara hi és?",
           "Em sent? Encara soc aquí quan vulgui."],
}

OPENING_RETRY: list[tuple[str, str]] = [
    ("Hello, is anyone there? Clínica Arenal, how can I help you?", "en"),
    ("¿Hay alguien al teléfono? Le escucho.", "es"),
]

RETRY: dict[str, str] = {
    "en": "Sorry, I didn't catch that. Could you say it again?",
    "es": "Perdone, no le he oído bien. ¿Me lo repite, por favor?",
    "ca": "Perdoni, no l'he sentit bé. M'ho pot repetir, si us plau?",
}

MODEL_DOWN: dict[str, str] = {
    "en": "Sorry, give me one moment. Could you say that again?",
    "es": "Perdone, un momento. ¿Me lo dice otra vez?",
    "ca": "Perdoni, un moment. M'ho pot repetir?",
}

HOLD: dict[str, str] = {
    "en": "One moment, please.",
    "es": "Un momento, por favor.",
    "ca": "Un moment, si us plau.",
}


def pick(table: dict[str, str], language: str) -> str:
    return table.get((language or "")[:2]) or table["en"]


def silence_prompt(language: str, attempt: int) -> str:
    lines = SILENCE_PROMPTS.get((language or "")[:2]) or SILENCE_PROMPTS["en"]
    return lines[min(attempt, len(lines) - 1)]


def fixed_lines(language: str) -> list[str]:
    """Everything above for one language, for warming a voice cache."""
    code = (language or "")[:2]
    return [
        *SILENCE_PROMPTS.get(code, []),
        RETRY.get(code, ""),
        MODEL_DOWN.get(code, ""),
        HOLD.get(code, ""),
    ] + [
        line for line, lang in OPENING_RETRY if lang == code
    ]
