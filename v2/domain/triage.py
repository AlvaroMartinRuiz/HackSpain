"""Routing a complaint, and the published red flags that are not bookable.

Which symptoms count as red flags is a published list, not a clinical
judgement, so it lives here as data and is matched rather than reasoned about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from v2.domain.identity import normalize_text

# Escalate, book nothing.
RED_FLAGS: tuple[tuple[str, str], ...] = (
    ("chest_pain", r"(chest.*(pain|tight|pressure)|pain across the chest|"
                   r"dolor.*pecho|opresi[oó]n.*pecho|pecho.*aprieta)"),
    ("stroke", r"(face.*droop|droopy|arm.*weak|slurred|words slurred|"
               r"cara.*ca[ií]da|habla.*arrastr|no puede hablar bien|media cara)"),
    ("breathless", r"(cannot get (their |my )?breath|can'?t breathe|struggling to breathe|"
                   r"out of nowhere.*breath|no puedo respirar|me falta el aire|ahogo)"),
    ("haemorrhage", r"(bleeding heavily|will not stop bleeding|won'?t stop bleeding|"
                    r"sangra mucho|no para de sangrar|hemorragia)"),
    ("head_injury", r"(banged (their |my )?head|hit (their|my) head|head injury|"
                    r"golpe en la cabeza|se dio un golpe.*cabeza).*"
                    r"(confus|being sick|vomit|sick since|mareado|v[oó]mit)"),
)

# The complaint a caller opens with, and the specialty it belongs to.
SYMPTOM_ROUTES: tuple[tuple[str, str], ...] = (
    ("orthopaedics", r"(went over on (their|my|his|her) ankle|twisted.*ankle|ankle.*swollen|"
                     r"tobillo|esguince)"),
    ("orthopaedics", r"(came off a bike|off (their|my) bike|cannot lift.*arm|can'?t lift.*arm|"
                     r"shoulder|hombro|bicicleta)"),
    ("orthopaedics", r"(knee|rodilla).*(click|lock|gave way|falla|traba)"),
    ("orthopaedics", r"(outstretched hand|wrist.*(pain|weak)|mu[nñ]eca)"),
    ("paediatrics", r"(child|kid|son|daughter|ni[nñ]o|ni[nñ]a|hijo|hija|beb[eé]).*"
                    r"(temperature|fever|fiebre|cough|tos|ear|o[ií]do|tummy|barriga|tripa)"),
    ("gynaecology", r"(heavy.*period|irregular period|bleeding between periods|"
                    r"regla|menstrua|sangrado entre)"),
    ("gynaecology", r"(pain low down on one side|dolor.*bajo vientre|ovario)"),
    ("general_practice", r"(tired and run down|run down|cansad|agotad|d[eé]bil)"),
    ("general_practice", r"(headache|dolor de cabeza|migra|jaqueca)"),
    ("general_practice", r"(sore throat|feverish|garganta|fiebre|resfriad|catarr)"),
    ("general_practice", r"(dizzy|mareo|maread)"),
)


@dataclass
class TriageResult:
    red_flag: Optional[str] = None
    specialty_id: Optional[str] = None
    matched: Optional[str] = None

    @property
    def escalate(self) -> bool:
        return self.red_flag is not None


def triage(complaint: str, is_child: Optional[bool] = None) -> TriageResult:
    """Route a described symptom, and catch the red flags first."""
    text = normalize_text(complaint)
    if not text:
        return TriageResult()

    for name, pattern in RED_FLAGS:
        if re.search(pattern, text):
            return TriageResult(red_flag=name, matched=name)

    for specialty_id, pattern in SYMPTOM_ROUTES:
        if re.search(pattern, text):
            if specialty_id == "paediatrics" and is_child is False:
                continue
            return TriageResult(specialty_id=specialty_id, matched=pattern[:40])

    # A general complaint with nothing specific in it: age decides the rest.
    if is_child is True:
        return TriageResult(specialty_id="paediatrics", matched="age_default")
    if is_child is False:
        return TriageResult(specialty_id="general_practice", matched="age_default")
    return TriageResult()
