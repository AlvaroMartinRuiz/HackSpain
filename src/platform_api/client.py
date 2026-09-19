"""HTTP client for the platform: clinic reads and call submissions.

Every call is timed and reported through ``on_event`` so the console can show
what the agent asked the clinic and what came back.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional, Sequence

import httpx

from src.config import settings

EventHook = Callable[[str, dict[str, Any]], Awaitable[None]]

SUBMIT_ROUTES = {
    "book": "/submit/book",
    "reschedule": "/submit/reschedule",
    "cancel": "/submit/cancel",
    "register": "/submit/register",
    "no_action": "/submit/no-action",
    "escalate": "/submit/escalate",
}

# Bounded so the worst case (every attempt hanging) still lands inside the 30 s
# window: 3 x 6 s of request plus 1.2 s of backoff is about 19 s.
SUBMIT_ATTEMPTS = 3
SUBMIT_BACKOFF_S = 0.4
SUBMIT_TIMEOUT_S = 6.0


class PlatformError(RuntimeError):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class SubmitResult:
    """Outcome of one submission attempt, kept whether it succeeded or not."""

    def __init__(self, action: str, payload: dict[str, Any], status: int, body: Any, elapsed_ms: int) -> None:
        self.action = action
        self.payload = payload
        self.status = status
        self.body = body
        self.elapsed_ms = elapsed_ms
        self.attempts = 1

    @property
    def accepted(self) -> bool:
        # Any 2xx, not just 200: a 201 read as a failure would fire the safety
        # net and pile a NO_ACTION on top of a good booking.
        return 200 <= self.status < 300

    @property
    def duplicate(self) -> bool:
        return self.status == 409

    @property
    def window_closed(self) -> bool:
        return self.status == 410

    @property
    def worth_retrying(self) -> bool:
        """Transport failures and platform faults only.

        404 (not our call), 410 (too late) and 422 (malformed) will not change
        however many times we ask.
        """
        return self.status == 0 or 500 <= self.status < 600

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "payload": self.payload,
            "status": self.status,
            "accepted": self.accepted,
            "body": self.body,
            "elapsed_ms": self.elapsed_ms,
            "attempts": self.attempts,
        }


class PlatformClient:
    def __init__(self, on_event: Optional[EventHook] = None) -> None:
        self._base = settings.api_base_url
        self._on_event = on_event
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=40)
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"X-Api-Key": settings.api_key},
            timeout=httpx.Timeout(12.0, connect=5.0),
            limits=limits,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._on_event is not None:
            await self._on_event(kind, payload)

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        started = time.perf_counter()
        try:
            response = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            await self._emit(
                "clinic_call",
                {"path": path, "params": params, "error": str(exc), "elapsed_ms": _ms(started)},
            )
            raise PlatformError(0, str(exc)) from exc

        elapsed = _ms(started)
        if response.status_code != 200:
            detail = _safe_json(response)
            await self._emit(
                "clinic_call",
                {"path": path, "params": params, "status": response.status_code,
                 "error": detail, "elapsed_ms": elapsed},
            )
            raise PlatformError(response.status_code, detail)

        body = response.json()
        await self._emit(
            "clinic_call",
            {"path": path, "params": params, "status": 200, "elapsed_ms": elapsed,
             "summary": _summarise(path, body)},
        )
        return body

    # ---- clinic reads -------------------------------------------------

    async def directory(
        self,
        name: Optional[str] = None,
        national_id: Optional[str] = None,
        phone: Optional[str] = None,
        date_of_birth: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        params = _compact({
            "name": name,
            "national_id": national_id,
            "phone": phone,
            "date_of_birth": date_of_birth,
        })
        if not params:
            return []
        body = await self._get("/directory", params)
        return body.get("matches", [])

    async def availability(
        self,
        date_from: str,
        date_to: str,
        provider_id: Optional[str] = None,
        specialty_id: Optional[str] = None,
        location_id: Optional[str] = None,
        patient_id: Optional[str] = None,
        insurer: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = _compact({
            "date_from": date_from,
            "date_to": date_to,
            "provider_id": provider_id,
            "specialty_id": specialty_id,
            "location_id": location_id,
            "patient_id": patient_id,
        })
        if insurer:
            params["insurer"] = list(insurer)
        return await self._get("/availability", params)

    async def appointments(self, patient_id: str, when: str = "upcoming") -> list[dict[str, Any]]:
        body = await self._get(f"/patients/{patient_id}/appointments", {"when": when})
        return body.get("appointments", [])

    async def clinic(self) -> dict[str, Any]:
        return await self._get("/clinic")

    async def submissions(self, limit: int = 50) -> list[dict[str, Any]]:
        body = await self._get("/submissions", {"limit": limit})
        return body.get("submissions", [])

    async def health(self) -> dict[str, Any]:
        return await self._get("/health")

    # ---- submissions -------------------------------------------------

    async def submit(self, action: str, payload: dict[str, Any]) -> SubmitResult:
        """Report one action, retrying the failures that a retry can fix.

        This is the only place the whole call turns into score, and the window
        stays open for 30 s after the socket closes, so a dropped connection
        should not be what loses a case.
        """
        path = SUBMIT_ROUTES.get(action)
        if path is None:
            raise ValueError(f"unknown submit action: {action}")

        started = time.perf_counter()
        result: Optional[SubmitResult] = None

        for attempt in range(SUBMIT_ATTEMPTS):
            try:
                response = await self._client.post(
                    path, json=payload, timeout=SUBMIT_TIMEOUT_S
                )
                status, body = response.status_code, _safe_json(response)
            except httpx.HTTPError as exc:
                status, body = 0, {"error": str(exc)}

            result = SubmitResult(action, payload, status, body, _ms(started))
            result.attempts = attempt + 1
            if not result.worth_retrying or attempt == SUBMIT_ATTEMPTS - 1:
                break
            await asyncio.sleep(SUBMIT_BACKOFF_S * (attempt + 1))

        assert result is not None
        await self._emit("submit", result.as_dict())
        return result


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "")}


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:400]


def _summarise(path: str, body: Any) -> dict[str, Any]:
    """A short shape of the response, so the console stays readable."""
    if not isinstance(body, dict):
        return {}
    if path == "/directory":
        matches = body.get("matches", [])
        return {
            "matches": len(matches),
            "top": [
                {
                    "patient_id": m.get("patient_id"),
                    "name": f"{m.get('given_name')} {m.get('first_surname')} {m.get('second_surname')}",
                    "dob": m.get("date_of_birth"),
                    "matched_fields": m.get("matched_fields"),
                }
                for m in matches[:4]
            ],
        }
    if path == "/availability":
        slots = body.get("slots", [])
        return {
            "slots": len(slots),
            "appointment_type": (body.get("appointment_type") or {}).get("id"),
            "blocked": body.get("blocked", []),
            "first_slot": slots[0].get("start_time") if slots else None,
        }
    if path.endswith("/appointments"):
        appointments = body.get("appointments", [])
        return {"appointments": len(appointments)}
    return {"keys": sorted(body.keys())[:8]}
