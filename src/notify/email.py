"""A clinic follow-up email after something is agreed on the call.

The scored record is already written. This only composes a summary, saves it
for the console, and sends it if a mail key is configured. A missing inbox
must never fail the call.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from uuid import uuid4

import httpx

from src.config import DATA_DIR, settings
from src.domain.timeref import now_madrid

log = logging.getLogger("socketwizard")

OUTBOX = DATA_DIR / "emails"

_SUBJECT = {
    "book": {
        "en": "Your appointment at Clínica Arenal",
        "es": "Su cita en Clínica Arenal",
        "ca": "La seva cita a Clínica Arenal",
    },
    "reschedule": {
        "en": "Your appointment has been moved",
        "es": "Su cita ha sido cambiada",
        "ca": "La seva cita ha canviat",
    },
    "cancel": {
        "en": "Your appointment has been cancelled",
        "es": "Su cita ha sido cancelada",
        "ca": "La seva cita ha estat anul·lada",
    },
    "register": {
        "en": "You are now on file at Clínica Arenal",
        "es": "Ya consta en Clínica Arenal",
        "ca": "Ja consta a Clínica Arenal",
    },
}

_HEADLINE = {
    "book": {"en": "Appointment confirmed", "es": "Cita confirmada", "ca": "Cita confirmada"},
    "reschedule": {"en": "Appointment moved", "es": "Cita cambiada", "ca": "Cita canviada"},
    "cancel": {"en": "Appointment cancelled", "es": "Cita cancelada", "ca": "Cita anul·lada"},
    "register": {"en": "Registration received", "es": "Alta registrada", "ca": "Alta registrada"},
}


def _lang(code: Optional[str]) -> str:
    return (code or "en")[:2] if (code or "en")[:2] in {"en", "es", "ca"} else "en"


def _pick(table: dict[str, dict[str, str]], kind: str, language: str) -> str:
    row = table.get(kind) or table["book"]
    return row.get(language) or row["en"]


def compose(
    kind: str,
    *,
    language: str,
    patient_name: str,
    details: dict[str, Any],
) -> dict[str, str]:
    """Build subject, text and HTML. No network."""
    language = _lang(language)
    subject = _pick(_SUBJECT, kind, language)
    headline = _pick(_HEADLINE, kind, language)
    rows = _rows(kind, language, details)
    greeting = {
        "en": f"Hello {patient_name}," if patient_name else "Hello,",
        "es": f"Hola {patient_name}:" if patient_name else "Hola:",
        "ca": f"Hola {patient_name}:" if patient_name else "Hola:",
    }[language]
    closing = {
        "en": "If anything is wrong, call us and we will sort it out.\n\nClínica Arenal",
        "es": "Si algo no encaja, llámenos y lo vemos.\n\nClínica Arenal",
        "ca": "Si alguna cosa no encaixa, truqueu-nos.\n\nClínica Arenal",
    }[language]
    text_lines = [greeting, "", headline, ""] + [f"{label}: {value}" for label, value in rows] + ["", closing]
    text = "\n".join(text_lines)
    maps = maps_url(str(details.get("address") or details.get("site") or ""))
    ics = ics_for(
        kind=kind,
        slot=str(details.get("slot_iso") or ""),
        site=str(details.get("site") or ""),
        doctor=str(details.get("doctor") or ""),
        patient_name=patient_name,
    )
    calendar_label = {"en": "Add to calendar", "es": "Añadir al calendario", "ca": "Afegir al calendari"}[language]
    directions_label = {"en": "Directions", "es": "Cómo llegar", "ca": "Com arribar-hi"}[language]
    button_bits = []
    if maps and kind in {"book", "reschedule"}:
        button_bits.append(
            f'<a href="{_esc(maps)}" style="display:inline-block;margin:16px 8px 0 0;padding:10px 14px;'
            f'background:#1b2430;color:#fff;border-radius:8px;text-decoration:none;font-weight:700">'
            f"{_esc(directions_label)}</a>"
        )
    if ics:
        button_bits.append(
            f'<span style="display:inline-block;margin:16px 8px 0 0;padding:10px 14px;'
            f'background:#f4f1ea;color:#1b2430;border-radius:8px;font-weight:700">'
            f"{_esc(calendar_label)} · ICS</span>"
        )
    buttons = f'<div>{"".join(button_bits)}</div>' if button_bits else ""
    cells = "".join(
        f"<tr><td style=\"padding:8px 0;color:#5c6570;width:38%\">{_esc(label)}</td>"
        f"<td style=\"padding:8px 0;color:#1b2430;font-weight:600\">{_esc(value)}</td></tr>"
        for label, value in rows
    )
    html = f"""<!doctype html>
<html><body style="margin:0;background:#f4f1ea;font-family:Georgia,serif;color:#1b2430">
  <div style="max-width:560px;margin:24px auto;background:#fff;border-radius:16px;padding:28px 32px;
              box-shadow:0 8px 30px rgba(40,32,20,.08)">
    <div style="letter-spacing:.14em;text-transform:uppercase;font-size:12px;color:#8a6a3b">Clínica Arenal</div>
    <h1 style="font-size:26px;margin:12px 0 8px">{_esc(headline)}</h1>
    <p style="margin:0 0 20px;color:#5c6570">{_esc(greeting)}</p>
    <table style="width:100%;border-collapse:collapse">{cells}</table>
    {buttons}
    <p style="margin:24px 0 0;color:#5c6570;white-space:pre-line">{_esc(closing)}</p>
  </div>
</body></html>"""
    return {"subject": subject, "text": text, "html": html, "ics": ics, "maps_url": maps}


def _rows(kind: str, language: str, details: dict[str, Any]) -> list[tuple[str, str]]:
    labels = {
        "when": {"en": "When", "es": "Cuándo", "ca": "Quan"},
        "doctor": {"en": "Doctor", "es": "Médico", "ca": "Metge"},
        "site": {"en": "Clinic", "es": "Centro", "ca": "Centre"},
        "name": {"en": "Name", "es": "Nombre", "ca": "Nom"},
        "phone": {"en": "Phone", "es": "Teléfono", "ca": "Telèfon"},
        "email": {"en": "Email", "es": "Correo", "ca": "Correu"},
        "insurer": {"en": "Insurer", "es": "Aseguradora", "ca": "Asseguradora"},
        "national_id": {"en": "ID", "es": "DNI/NIE", "ca": "DNI/NIE"},
        "dob": {"en": "Date of birth", "es": "Fecha de nacimiento", "ca": "Data de naixement"},
    }

    def lab(key: str) -> str:
        return labels[key][language]

    rows: list[tuple[str, str]] = []
    if kind in {"book", "reschedule"}:
        if details.get("when"):
            rows.append((lab("when"), str(details["when"])))
        if details.get("doctor"):
            rows.append((lab("doctor"), str(details["doctor"])))
        if details.get("site"):
            rows.append((lab("site"), str(details["site"])))
    elif kind == "cancel":
        if details.get("when"):
            rows.append((lab("when"), str(details["when"])))
        if details.get("doctor"):
            rows.append((lab("doctor"), str(details["doctor"])))
    elif kind == "register":
        if details.get("name"):
            rows.append((lab("name"), str(details["name"])))
        if details.get("national_id"):
            rows.append((lab("national_id"), str(details["national_id"])))
        if details.get("date_of_birth"):
            rows.append((lab("dob"), str(details["date_of_birth"])))
        if details.get("phone"):
            rows.append((lab("phone"), str(details["phone"])))
        if details.get("email"):
            rows.append((lab("email"), str(details["email"])))
        if details.get("insurer"):
            rows.append((lab("insurer"), str(details["insurer"])))
    if not rows:
        rows.append((
            {"en": "Note", "es": "Nota", "ca": "Nota"}[language],
            {"en": "We have recorded what we agreed on the call.",
             "es": "Hemos anotado lo acordado en la llamada.",
             "ca": "Hem anotat el que hem acordat."}[language],
        ))
    return rows


def save_copy(call_id: str, kind: str, message: dict[str, str]) -> Path:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    path = OUTBOX / f"{call_id}-{kind}.html"
    path.write_text(message["html"], encoding="utf-8")
    if message.get("ics"):
        (OUTBOX / f"{call_id}-{kind}.ics").write_text(message["ics"], encoding="utf-8")
    return path


def maps_url(place: str) -> str:
    """A pin on a real street, never a city-wide Madrid search."""
    text = _pin_address(place)
    if not text:
        return ""
    return "https://www.google.com/maps/search/?api=1&query=" + quote(text)


# Clínica Arenal sites mapped onto real streets so Directions is usable in a demo.
_SITE_PIN = {
    "norte": "Hospital Universitario La Paz, Paseo de la Castellana 261, 28046 Madrid",
    "arenal norte": "Hospital Universitario La Paz, Paseo de la Castellana 261, 28046 Madrid",
    "centro": "Clínica Arenal Centro, Calle del Arenal 12, 28013 Madrid",
    "arenal centro": "Clínica Arenal Centro, Calle del Arenal 12, 28013 Madrid",
    "sur": "Clínica Arenal Sur, Avenida de las Ciudades 8, 28903 Getafe, Madrid",
    "arenal sur": "Clínica Arenal Sur, Avenida de las Ciudades 8, 28903 Getafe, Madrid",
    "alberto alcocer": "Hospital Universitario La Paz, Paseo de la Castellana 261, 28046 Madrid",
    "madrid": "Hospital Universitario La Paz, Paseo de la Castellana 261, 28046 Madrid",
}


def _pin_address(place: str) -> str:
    text = (place or "").strip()
    if not text:
        return ""
    key = text.lower()
    if key in _SITE_PIN:
        return _SITE_PIN[key]
    for token, pin in sorted(_SITE_PIN.items(), key=lambda item: -len(item[0])):
        if token != "madrid" and token in key:
            return pin
    if key in {"madrid", "madrid, spain", "españa", "spain"}:
        return _SITE_PIN["madrid"]
    if not any(ch.isdigit() for ch in text):
        return _SITE_PIN["madrid"]
    return text


def ics_for(
    *,
    kind: str,
    slot: str,
    site: str = "",
    doctor: str = "",
    patient_name: str = "",
    call_id: str = "",
) -> str:
    """VEVENT for book/reschedule. Empty for anything else. No invented reference."""
    if kind not in {"book", "reschedule"} or not slot:
        return ""
    try:
        start = datetime.fromisoformat(slot.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if start.tzinfo is None:
        return ""
    uid = f"{call_id or uuid4()}@socketwizard"
    summary = "Clínica Arenal"
    description = f"Appointment with {doctor or 'your clinician'}."
    if patient_name:
        description += f" Patient: {patient_name}."
    description += " No booking reference was issued."
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    def _fmt(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _esc_ics(value: str) -> str:
        return (
            str(value or "")
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
        )

    return "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Socket Wizard//Clínica Arenal//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_fmt(start)}",
        f"SUMMARY:{_esc_ics(summary)}",
        f"DESCRIPTION:{_esc_ics(description)}",
        f"LOCATION:{_esc_ics(site)}",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])


async def deliver(to: Optional[str | list[str]], message: dict[str, str]) -> dict[str, Any]:
    """Send if a provider is configured. Always safe to call.

    One recipient per request: Resend's test domain rejects the whole batch
    if any address is not the account inbox.
    """
    raw = to if isinstance(to, list) else [to]
    recipients: list[str] = []
    for addr in (settings.followup_copy, *raw):
        if addr and "@" in addr and addr not in recipients:
            recipients.append(addr)
    if not settings.followup_email or not recipients:
        return {"sent": False, "reason": "no inbox", "to": recipients}
    if not settings.resend_api_key and not _smtp_ready():
        return {"sent": False, "reason": "preview only", "to": recipients}
    sent_to: list[str] = []
    last_error = ""
    for addr in recipients:
        error = await _send_one(addr, message)
        if error is None:
            sent_to.append(addr)
        else:
            last_error = error
            log.warning("followup to %s failed: %s", addr, error)
    if sent_to:
        return {"sent": True, "reason": "sent", "to": sent_to}
    return {"sent": False, "reason": last_error or "not delivered", "to": recipients}


def _smtp_ready() -> bool:
    return bool(settings.smtp_user and settings.smtp_password and "@" in settings.smtp_user)


async def _send_one(addr: str, message: dict[str, str]) -> Optional[str]:
    """Return None on success, otherwise the error. Resend first, Gmail SMTP if it refuses."""
    resend_error = await _send_resend(addr, message)
    if resend_error is None:
        return None
    if _smtp_ready():
        return await asyncio.to_thread(_send_smtp, addr, message)
    return resend_error


async def _send_resend(addr: str, message: dict[str, str]) -> Optional[str]:
    if not settings.resend_api_key:
        return "no resend key"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=_resend_payload(addr, message),
            )
        if response.status_code >= 300:
            return f"HTTP {response.status_code} {response.text[:180]}"
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _resend_payload(addr: str, message: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "from": settings.followup_from,
        "to": [addr],
        "subject": message["subject"],
        "html": message["html"],
        "text": message["text"],
    }
    ics = message.get("ics") or ""
    if ics:
        payload["attachments"] = [{
            "filename": "cita.ics",
            "content": base64.b64encode(ics.encode("utf-8")).decode("ascii"),
        }]
    return payload


def _send_smtp(addr: str, message: dict[str, str]) -> Optional[str]:
    mixed = MIMEMultipart("mixed")
    mixed["Subject"] = message["subject"]
    mixed["From"] = f"Clínica Arenal <{settings.smtp_user}>"
    mixed["To"] = addr
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(message["text"], "plain", "utf-8"))
    alternative.attach(MIMEText(message["html"], "html", "utf-8"))
    mixed.attach(alternative)
    ics = message.get("ics") or ""
    if ics:
        part = MIMEApplication(ics.encode("utf-8"), _subtype="ics")
        part.add_header("Content-Disposition", "attachment", filename="cita.ics")
        mixed.attach(part)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=8) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.sendmail(settings.smtp_user, [addr], mixed.as_string())
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


_WEEKDAYS = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
    "ca": ("dilluns", "dimarts", "dimecres", "dijous", "divendres", "dissabte", "diumenge"),
}
_MONTHS = {
    "en": ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"),
    "es": ("enero", "febrero", "marzo", "abril", "mayo", "junio",
           "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"),
    "ca": ("gener", "febrer", "març", "abril", "maig", "juny",
           "juliol", "agost", "setembre", "octubre", "novembre", "desembre"),
}


def format_when(slot: Optional[str], language: str) -> str:
    if not slot:
        return ""
    try:
        moment = datetime.fromisoformat(slot.replace("Z", "+00:00")).astimezone(now_madrid().tzinfo)
    except ValueError:
        return slot
    language = _lang(language)
    weekday = _WEEKDAYS[language][moment.weekday()]
    month = _MONTHS[language][moment.month - 1]
    day = moment.day
    clock = moment.strftime("%H:%M")
    if language == "es":
        return f"{weekday} {day} de {month}, {clock}"
    if language == "ca":
        return f"{weekday} {day} de {month}, {clock}"
    return f"{weekday} {day} {month}, {clock}"


def _esc(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
