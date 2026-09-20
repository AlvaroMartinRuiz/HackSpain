"""Prueba rápida: API key + conexión con la plataforma Prosper."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_KEY = os.getenv("PLATFORM_API_KEY") or os.getenv("PROSPER_API_KEY", "")
BASE_URL = (
    os.getenv("PLATFORM_API_BASE_URL")
    or os.getenv("PROSPER_BASE_URL")
    or "https://hackspain.getprosperapp.com/api/v1"
).rstrip("/")


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "OK" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    print(f"Base URL: {BASE_URL}")
    print(f"API key:  {'set' if API_KEY else 'MISSING'}")
    print()

    if not API_KEY:
        check("API key configurada", False, "Añade PLATFORM_API_KEY o PROSPER_API_KEY en .env")
        return 1

    passed = 0
    total = 0

    with httpx.Client(timeout=20.0) as client:
        # Health sin key (según docs)
        total += 1
        try:
            r = client.get(f"{BASE_URL}/health")
            passed += check("GET /health (sin key)", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as e:
            check("GET /health (sin key)", False, str(e))

        # Health con key
        total += 1
        try:
            r = client.get(f"{BASE_URL}/health", headers={"X-Api-Key": API_KEY})
            passed += check("GET /health (con key)", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as e:
            check("GET /health (con key)", False, str(e))

        # Directory — endpoint clave del hackathon
        total += 1
        try:
            r = client.get(
                f"{BASE_URL}/directory",
                headers={"X-Api-Key": API_KEY},
                params={"name": "Marta Ruiz"},
            )
            if r.status_code == 200:
                data = r.json()
                n = len(data) if isinstance(data, list) else len(data.get("patients", data))
                passed += check("GET /directory", True, f"HTTP 200, {n} resultado(s)")
            elif r.status_code == 403:
                check("GET /directory", False, "403 Invalid API key — key inválida o host incorrecto")
            else:
                check("GET /directory", False, f"HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            check("GET /directory", False, str(e))

        # Catálogo clínica
        total += 1
        try:
            r = client.get(f"{BASE_URL}/clinic", headers={"X-Api-Key": API_KEY})
            if r.status_code == 200:
                passed += check("GET /clinic", True, "HTTP 200 — catálogo accesible")
            elif r.status_code == 403:
                check("GET /clinic", False, "403 Invalid API key")
            else:
                check("GET /clinic", False, f"HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            check("GET /clinic", False, str(e))

    print()
    print(f"Resultado: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
