"""Coarse coordinates for Madrid and its suburbs.

Site choice is decided by straight-line distance from published coordinates,
and the origins are chosen so the nearest site wins by a clear margin — so a
municipality or district centroid resolves them without a network call.
"""

from __future__ import annotations

from typing import Optional

from src.domain.identity import normalize_text

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


def locate(text: str) -> Optional[tuple[str, float, float]]:
    """Find the place named in an address, and its coordinates.

    An address reads street first and town last, and a street can carry a town's
    name, so the last and most specific thing said wins.
    """
    normalized = normalize_text(text)
    if not normalized:
        return None

    found: list[tuple[str, int, int]] = []
    for place in PLACES:
        index = normalized.rfind(place)
        if index >= 0:
            found.append((place, index, len(place)))
    for alias, canonical in _ALIASES.items():
        index = normalized.rfind(alias)
        if index >= 0 and canonical not in normalized:
            found.append((canonical, index, len(alias)))

    if not found:
        return None

    specific = [entry for entry in found if entry[0] not in _GENERIC] or found
    # Latest mention first, then the longer name where two start together.
    specific.sort(key=lambda entry: (entry[1], entry[2]), reverse=True)
    place = specific[0][0]
    latitude, longitude = PLACES[place]
    return place, latitude, longitude
