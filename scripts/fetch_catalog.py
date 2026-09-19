"""Cache the clinic catalogue to data/catalog.json.

The catalogue is generated once for the whole event, so it is pulled here and
committed rather than fetched on every boot.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "v2" / ".env", override=True)

API_KEY = os.getenv("PLATFORM_API_KEY", "")
BASE_URL = (os.getenv("PLATFORM_API_BASE_URL") or "").rstrip("/")
OUT = ROOT / "data" / "catalog.json"

ENDPOINTS = {
    "clinic": "/clinic",
    "providers": "/providers",
    "locations": "/locations",
    "specialties": "/specialties",
    "appointment_types": "/appointment-types",
    "insurance_plans": "/insurance-plans",
}


def main() -> int:
    if not API_KEY or not BASE_URL:
        print("PLATFORM_API_KEY and PLATFORM_API_BASE_URL must be set in .env")
        return 1

    catalog: dict[str, object] = {}
    with httpx.Client(timeout=30.0, headers={"X-Api-Key": API_KEY}) as client:
        for name, path in ENDPOINTS.items():
            response = client.get(f"{BASE_URL}{path}")
            response.raise_for_status()
            catalog[name] = response.json()
            print(f"[OK] {path}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
