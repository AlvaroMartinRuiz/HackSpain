"""Coarse coordinates for Madrid and its suburbs.

Site choice is decided by straight-line distance from published coordinates,
and the origins are chosen so the nearest site wins by a clear margin. Towns
and districts are enough for that — but the public cases name streets and
landmarks, so those have to resolve too, without a network call.
"""

from __future__ import annotations

import re
from typing import Optional

from v2.domain.identity import normalize_text

PLACES: dict[str, tuple[float, float]] = {
    # Madrid districts
    "centro": (40.4155, -3.7074),
    "arganzuela": (40.3964, -3.6959),
    "retiro": (40.4085, -3.6766),
    "salamanca": (40.4300, -3.6780),
    "chamartin": (40.4600, -3.6770),
    "tetuan": (40.4600, -3.7000),
    "chamberi": (40.4350, -3.7030),
    "fuencarral": (40.4780, -3.7090),
    "el pardo": (40.5180, -3.7740),
    "moncloa": (40.4350, -3.7200),
    "aravaca": (40.4560, -3.7830),
    "latina": (40.3940, -3.7460),
    "carabanchel": (40.3800, -3.7280),
    "usera": (40.3810, -3.7060),
    "puente de vallecas": (40.3900, -3.6690),
    "moratalaz": (40.4070, -3.6450),
    "ciudad lineal": (40.4480, -3.6520),
    "hortaleza": (40.4700, -3.6400),
    "villaverde": (40.3450, -3.6940),
    "villa de vallecas": (40.3790, -3.6180),
    "vicalvaro": (40.4040, -3.6080),
    "san blas": (40.4290, -3.6120),
    "barajas": (40.4730, -3.5800),
    "madrid": (40.4168, -3.7038),
    # Southern belt
    "getafe": (40.3080, -3.7320),
    "leganes": (40.3270, -3.7630),
    "alcorcon": (40.3460, -3.8250),
    "mostoles": (40.3230, -3.8650),
    "fuenlabrada": (40.2840, -3.7940),
    "parla": (40.2370, -3.7680),
    "pinto": (40.2420, -3.7000),
    "valdemoro": (40.1900, -3.6740),
    "humanes": (40.2520, -3.8300),
    "grinon": (40.2100, -3.8400),
    "aranjuez": (40.0310, -3.6030),
    "navalcarnero": (40.2880, -4.0120),
    "villaviciosa de odon": (40.3570, -3.9000),
    # Eastern corridor
    "alcala de henares": (40.4820, -3.3640),
    "torrejon de ardoz": (40.4590, -3.4790),
    "coslada": (40.4240, -3.5600),
    "san fernando de henares": (40.4230, -3.5300),
    "rivas vaciamadrid": (40.3320, -3.5180),
    "arganda del rey": (40.3010, -3.4380),
    "mejorada del campo": (40.3940, -3.4870),
    # Northern corridor
    "alcobendas": (40.5470, -3.6420),
    "san sebastian de los reyes": (40.5470, -3.6260),
    "tres cantos": (40.6010, -3.7100),
    "colmenar viejo": (40.6590, -3.7660),
    "algete": (40.5960, -3.4990),
    # Western corridor
    "pozuelo de alarcon": (40.4330, -3.8130),
    "majadahonda": (40.4730, -3.8720),
    "las rozas": (40.4920, -3.8740),
    "boadilla del monte": (40.4060, -3.8770),
    "villanueva de la canada": (40.4460, -3.9930),
    "brunete": (40.4050, -4.0140),
    "galapagar": (40.5800, -4.0000),
    "collado villalba": (40.6350, -4.0050),
    "san lorenzo de el escorial": (40.5900, -4.1480),
}

# Named points the public cases actually say, plus a few that private cases
# can draw from without leaving the same three catchment areas.
LANDMARKS: dict[str, tuple[float, float]] = {
    "puerta del sol": (40.4169, -3.7033),
    "calle de preciados": (40.4186, -3.7058),
    "preciados": (40.4186, -3.7058),
    "gran via": (40.4200, -3.7014),
    "callao": (40.4200, -3.7056),
    "plaza mayor": (40.4154, -3.7074),
    "plaza de espana": (40.4233, -3.7122),
    "plaza de castilla": (40.4663, -3.6894),
    "paseo de la castellana": (40.4470, -3.6905),
    "castellana": (40.4470, -3.6905),
    "nuevos ministerios": (40.4465, -3.6922),
    "santiago bernabeu": (40.4531, -3.6883),
    "cuatro torres": (40.4760, -3.6875),
    "alberto alcocer": (40.4645, -3.6836),
    "atocha": (40.4065, -3.6895),
    "lavapies": (40.4089, -3.7009),
    "malasana": (40.4267, -3.7040),
    "chueca": (40.4227, -3.6975),
    "azca": (40.4489, -3.6936),
    "ciudad lineal": (40.4480, -3.6520),
}

# "Calle de Madrid, Getafe" is in Getafe, so the city name on its own only
# counts when nothing more specific was said.
_GENERIC = {"madrid"}

_ALIASES: dict[str, str] = {
    "rivas": "rivas vaciamadrid",
    "vallecas": "puente de vallecas",
    "alcala": "alcala de henares",
    "torrejon": "torrejon de ardoz",
    "sanse": "san sebastian de los reyes",
    "pozuelo": "pozuelo de alarcon",
    "el escorial": "san lorenzo de el escorial",
    "villalba": "collado villalba",
    "boadilla": "boadilla del monte",
    "arganda": "arganda del rey",
}

# Exact codes that pin a district or town; prefixes cover the rest of the belt.
_POSTCODES: dict[str, str] = {
    "28001": "centro", "28002": "retiro", "28004": "centro", "28005": "centro",
    "28008": "moncloa", "28012": "centro", "28013": "centro", "28014": "centro",
    "28015": "centro", "28003": "chamberi", "28010": "chamberi",
    "28006": "salamanca", "28007": "retiro", "28009": "retiro",
    "28016": "chamartin", "28036": "chamartin", "28046": "chamartin",
    "28020": "tetuan", "28029": "tetuan", "28039": "tetuan",
    "28021": "villaverde", "28041": "villaverde",
    "28025": "carabanchel", "28044": "carabanchel", "28047": "latina",
    "28026": "usera", "28019": "usera",
    "28031": "villa de vallecas", "28038": "puente de vallecas",
    "28018": "puente de vallecas", "28053": "puente de vallecas",
    "28032": "vicalvaro", "28030": "moratalaz",
    "28022": "san blas", "28037": "san blas",
    "28027": "ciudad lineal", "28017": "ciudad lineal",
    "28033": "hortaleza", "28043": "hortaleza", "28042": "barajas",
    "28034": "fuencarral", "28035": "moncloa", "28011": "moncloa",
    "28040": "moncloa", "28045": "arganzuela", "28028": "salamanca",
    "28901": "getafe", "28902": "getafe", "28903": "getafe",
    "28904": "getafe", "28905": "getafe",
    "28910": "leganes", "28911": "leganes", "28912": "leganes",
    "28913": "leganes", "28914": "leganes", "28915": "leganes",
    "28921": "alcorcon", "28922": "alcorcon", "28923": "alcorcon",
    "28924": "alcorcon", "28925": "alcorcon",
    "28100": "alcobendas", "28700": "san sebastian de los reyes",
    "28760": "tres cantos",
    "28801": "alcala de henares", "28802": "alcala de henares",
    "28805": "alcala de henares", "28806": "alcala de henares",
    "28850": "torrejon de ardoz", "28820": "coslada",
    "28521": "rivas vaciamadrid", "28522": "rivas vaciamadrid",
    "28980": "parla", "28981": "parla",
    "28940": "fuenlabrada", "28941": "fuenlabrada", "28942": "fuenlabrada",
    "28943": "fuenlabrada", "28944": "fuenlabrada", "28945": "fuenlabrada",
    "28930": "mostoles", "28931": "mostoles", "28932": "mostoles",
    "28933": "mostoles", "28934": "mostoles", "28935": "mostoles",
    "28936": "mostoles", "28937": "mostoles", "28938": "mostoles",
    "28220": "majadahonda", "28221": "majadahonda", "28222": "majadahonda",
    "28290": "las rozas", "28291": "las rozas", "28292": "las rozas",
    "28223": "pozuelo de alarcon", "28224": "pozuelo de alarcon",
}

_POSTCODE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("2890", "getafe"),
    ("2891", "leganes"),
    ("2892", "alcorcon"),
    ("2893", "mostoles"),
    ("2894", "fuenlabrada"),
    ("2898", "parla"),
    ("2880", "alcala de henares"),
    ("2885", "torrejon de ardoz"),
    ("2882", "coslada"),
    ("2852", "rivas vaciamadrid"),
    ("2810", "alcobendas"),
    ("2876", "tres cantos"),
    ("2822", "pozuelo de alarcon"),
    ("2829", "las rozas"),
)

# A town name sitting in "Calle de …" is the street, not the municipality.
_STREET_PREFIX = re.compile(
    r"(?:calle|c/|avenida|avda|av\.?|paseo|plaza|ronda|carretera|camino|via)"
    r"(?:\s+de(?:\s+la|\s+las|\s+los|\s+el)?)?\s+$"
)


def locate(text: str) -> Optional[tuple[str, float, float]]:
    """Find the place named in an address, and its coordinates.

    An address reads street first and town last. A street can carry a town's
    name ("Calle de Madrid", "Calle de Alcalá"), so the last and most specific
    thing said wins — a landmark or a postcode outranks a city centroid.
    """
    normalized = normalize_text(text)
    if not normalized:
        return None
    normalized = re.sub(r"\bcentre\b", "centro", normalized)
    normalized = re.sub(r"\bdowntown\b", "centro", normalized)

    # specificity, index, name, lat, lon — highest spec, then latest mention.
    found: list[tuple[int, int, str, float, float]] = []

    for code in re.findall(r"\b(\d{5})\b", text):
        place = _place_from_postcode(code)
        if place is None or place not in PLACES:
            continue
        latitude, longitude = PLACES[place]
        index = normalized.rfind(code)
        found.append((80, max(index, 0), place, latitude, longitude))

    for name, (latitude, longitude) in LANDMARKS.items():
        index = _find_phrase(normalized, name)
        if index >= 0:
            found.append((70 + min(len(name), 20), index, name, latitude, longitude))

    for place, (latitude, longitude) in PLACES.items():
        index = _find_phrase(normalized, place)
        if index < 0:
            continue
        if " " not in place and _is_street_name(normalized, index):
            continue
        spec = 20 if place in _GENERIC else 50 + min(len(place), 15)
        found.append((spec, index, place, latitude, longitude))

    for alias, canonical in _ALIASES.items():
        if _find_phrase(normalized, canonical) >= 0:
            continue
        index = _find_phrase(normalized, alias)
        if index < 0 or _is_street_name(normalized, index):
            continue
        latitude, longitude = PLACES[canonical]
        found.append((45, index, canonical, latitude, longitude))

    if not found:
        return None

    found.sort(key=lambda entry: (entry[0], entry[1], len(entry[2])), reverse=True)
    _spec, _index, name, latitude, longitude = found[0]
    return name, latitude, longitude


def _place_from_postcode(code: str) -> Optional[str]:
    if code in _POSTCODES:
        return _POSTCODES[code]
    for prefix, place in _POSTCODE_PREFIXES:
        if code.startswith(prefix):
            return place
    if code.startswith("280"):
        return "madrid"
    return None


def _find_phrase(text: str, phrase: str) -> int:
    match = re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", text)
    return match.start() if match else -1


def _is_street_name(text: str, index: int) -> bool:
    """True when this match is the name of a street, not a town."""
    return bool(_STREET_PREFIX.search(text[max(0, index - 48):index]))
