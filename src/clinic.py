from datetime import datetime, timedelta
from typing import Any

# Datos de prueba locales hasta conectar el EHR real del hackathon.
PATIENTS: list[dict[str, Any]] = [
    {
        "id": "PAT-001",
        "first_name": "Ana",
        "last_name": "Garcia",
        "date_of_birth": "1990-03-12",
        "phone": "+34600111222",
    },
    {
        "id": "PAT-002",
        "first_name": "Luis",
        "last_name": "Martinez",
        "date_of_birth": "1985-07-21",
        "phone": "+34600333444",
    },
]

APPOINTMENTS: list[dict[str, Any]] = [
    {
        "id": "APT-001",
        "patient_id": "PAT-001",
        "doctor": "Dr. Lopez",
        "site": "Madrid Centro",
        "starts_at": (datetime.now() + timedelta(days=3)).replace(hour=10, minute=0).isoformat(),
    }
]

SLOTS = [
    {
        "starts_at": (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0).isoformat(),
        "doctor": "Dr. Lopez",
        "site": "Madrid Centro",
    },
    {
        "starts_at": (datetime.now() + timedelta(days=1)).replace(hour=11, minute=30).isoformat(),
        "doctor": "Dr. Ruiz",
        "site": "Madrid Norte",
    },
]


def find_patients(name: str | None = None, phone: str | None = None, dob: str | None = None) -> list[dict]:
    results = PATIENTS
    if name:
        needle = name.lower()
        results = [p for p in results if needle in f"{p['first_name']} {p['last_name']}".lower()]
    if phone:
        results = [p for p in results if p["phone"] == phone]
    if dob:
        results = [p for p in results if p["date_of_birth"] == dob]
    return results


def list_availability() -> list[dict]:
    return SLOTS


def book_appointment(patient_id: str, starts_at: str, doctor: str, site: str) -> dict:
    appointment = {
        "id": f"APT-{len(APPOINTMENTS) + 1:03d}",
        "patient_id": patient_id,
        "doctor": doctor,
        "site": site,
        "starts_at": starts_at,
    }
    APPOINTMENTS.append(appointment)
    return appointment
