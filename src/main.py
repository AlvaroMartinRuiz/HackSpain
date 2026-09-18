import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from src import clinic
from src.config import PROSPER_API_KEY, PORT
from src.prosper_client import ProsperClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hackspain")

app = FastAPI(
    title="HackSpain Prosper Agent",
    description="Endpoint base para el agente de voz de recepcion de clinica",
    version="0.1.0",
)
prosper = ProsperClient()


class BookRequest(BaseModel):
    patient_id: str
    starts_at: str
    doctor: str
    site: str


class CallOutcome(BaseModel):
    action: str = Field(description="booked | rescheduled | cancelled | refused | transferred")
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "hackspain-prosper-agent"}


@app.get("/api/prosper/check")
async def prosper_check() -> dict[str, Any]:
    if not PROSPER_API_KEY:
        raise HTTPException(status_code=500, detail="PROSPER_API_KEY no configurada")

    result: dict[str, Any] = {
        "api_key_configured": True,
        "prosper_connected": False,
        "note": "Si ves 403, la key puede estar pendiente de activacion por los organizadores.",
    }

    try:
        result["health"] = await prosper.health()
        result["prosper_connected"] = True
    except Exception as health_error:
        result["health_error"] = str(health_error)

    try:
        logs = await prosper.list_call_logs(limit=3)
        result["recent_call_logs"] = logs.get("call_logs", [])[:3]
        result["prosper_connected"] = True
    except Exception as logs_error:
        result["call_logs_error"] = str(logs_error)

    return result


@app.post("/webhook")
async def webhook(request: Request) -> dict[str, str]:
    payload = await request.json()
    logger.info("Webhook recibido: %s", payload)
    return {"status": "received"}


@app.get("/api/patients/search")
async def search_patients(name: str | None = None, phone: str | None = None, dob: str | None = None) -> dict:
    return {"patients": clinic.find_patients(name=name, phone=phone, dob=dob)}


@app.get("/api/availability")
async def availability() -> dict:
    return {"slots": clinic.list_availability()}


@app.post("/api/appointments")
async def create_appointment(body: BookRequest) -> dict:
    appointment = clinic.book_appointment(
        patient_id=body.patient_id,
        starts_at=body.starts_at,
        doctor=body.doctor,
        site=body.site,
    )
    return {"appointment": appointment}


@app.post("/api/call-outcome")
async def report_call_outcome(outcome: CallOutcome) -> dict[str, Any]:
    logger.info("Resultado de llamada: %s", outcome.model_dump())
    return {"recorded": True, "outcome": outcome}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=PORT, reload=True)
