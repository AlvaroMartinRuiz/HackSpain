from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

Language = Literal["en", "es", "ca"]
Action = Literal["book", "cancel", "reschedule", "register", "no_action", "escalate"]
Mode = Literal["simulation", "practice", "live"]
Identifier = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Identity(Model):
    name: str | None = Field(default=None, max_length=150)
    date_of_birth: str | None = Field(default=None, max_length=10)
    national_id: str | None = Field(default=None, max_length=25)
    phone: str | None = Field(default=None, max_length=25)


class Registration(Model):
    given_name: str = Field(min_length=1, max_length=100)
    first_surname: str = Field(min_length=1, max_length=100)
    second_surname: str = Field(min_length=1, max_length=100)
    national_id: str = Field(min_length=1, max_length=32)
    date_of_birth: str = Field(min_length=10, max_length=10)
    phone: str = Field(min_length=1, max_length=32)
    email: str = Field(min_length=3, max_length=254)
    insurer: str = Field(min_length=1, max_length=80)


class RegistrationFields(Model):
    given_name: str | None = Field(default=None, max_length=100)
    first_surname: str | None = Field(default=None, max_length=100)
    second_surname: str | None = Field(default=None, max_length=100)
    national_id: str | None = Field(default=None, max_length=32)
    date_of_birth: str | None = Field(default=None, max_length=10)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    insurer: str | None = Field(default=None, max_length=80)


class SchedulingCriteria(Model):
    specialty_id: Literal["general_practice", "paediatrics", "dermatology", "orthopaedics",
                          "gynaecology", "physiotherapy"] | None = None
    when: str | None = Field(default=None, max_length=250)
    doctor_name: str | None = Field(default=None, max_length=100)
    location_id: Literal["centro", "norte", "sur"] | None = None
    appointment_id: Identifier | None = None
    appointment_when: str | None = Field(default=None, max_length=250)
    appointment_doctor_name: str | None = Field(default=None, max_length=100)
    appointment_location_id: Literal["centro", "norte", "sur"] | None = None
    part_of_day: Literal["morning", "afternoon"] | None = None
    time_of_day: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")] | None = None
    appointment_time: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")] | None = None
    insurers: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list, max_length=5)
    clinician_language: Language | None = None


class Operation(Model):
    op: Literal["create", "identify", "prepare", "confirm", "refuse", "escalate", "facts", "ask", "finish"]
    question: Literal["identity", "appointment", "registration_identity", "registration_contact", "confirm", "clarify"] | None = None
    intent_id: Identifier | None = None
    action: Action | None = None
    subject: str | None = Field(default=None, max_length=150)
    evidence: str = Field(default="", max_length=1500)
    identity: Identity | None = None
    specialty_id: Literal["general_practice", "paediatrics", "dermatology", "orthopaedics",
                          "gynaecology", "physiotherapy"] | None = None
    when: str | None = Field(default=None, max_length=250)
    doctor_name: str | None = Field(default=None, max_length=100)
    location_id: Literal["centro", "norte", "sur"] | None = None
    appointment_id: str | None = Field(default=None, max_length=80)
    registration: Registration | None = None
    registration_fields: RegistrationFields | None = None
    criteria: SchedulingCriteria | None = None
    clear_fields: list[Literal["specialty_id", "when", "doctor_name", "location_id", "appointment_id",
                               "appointment_when", "appointment_doctor_name", "appointment_location_id",
                               "part_of_day", "time_of_day", "appointment_time", "insurers", "clinician_language"]] = Field(default_factory=list, max_length=13)
    option: int | None = Field(default=None, ge=1, le=10)
    offer_revision: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=80)
    topic: Literal["sites", "hours", "specialties", "plans", "doctors"] | None = None


class TurnDecision(Model):
    language: Language
    operations: list[Operation] = Field(default_factory=list, max_length=8)


class Offer(Model):
    offer_id: str = Field(default_factory=lambda: uuid4().hex)
    revision: int
    action: Action
    payload: dict
    display: dict
    presented_turn: int | None = None


class Intent(Model):
    intent_id: Identifier
    action: Action
    subject: str
    status: Literal["collecting", "awaiting_confirmation", "completed", "blocked"] = "collecting"
    patient: dict | None = None
    identity_inputs: dict = Field(default_factory=dict)
    identity_status: Literal["partial", "verified", "not_found", "ambiguous", "invalid"] = "partial"
    criteria: SchedulingCriteria = Field(default_factory=SchedulingCriteria)
    registration_fields: RegistrationFields = Field(default_factory=RegistrationFields)
    missing_fields: list[str] = Field(default_factory=list)
    validation_errors: dict[str, str] = Field(default_factory=dict)
    choices: list[dict] = Field(default_factory=list)
    submission_uncertain: bool = False
    revision: int = 0
    offers: list[Offer] = Field(default_factory=list)
    blocking_reason: str | None = None
    receipts: list[dict] = Field(default_factory=list)


class Reply(Model):
    response_id: str = Field(default_factory=lambda: uuid4().hex)
    text: str
    language: Language
    epoch: int
    offers: dict[str, int] = Field(default_factory=dict)
    completion: bool = False


class CallState(Model):
    schema_version: Literal[1] = 1
    run_id: Identifier = Field(default_factory=lambda: uuid4().hex)
    call_id: Identifier
    mode: Mode = "simulation"
    language: Language = "en"
    reference_time: datetime
    turn: int = 0
    revision: int = 0
    intents: dict[str, Intent] = Field(default_factory=dict)
    completion_requested: bool = False
    emergency: bool = False
    history: list[dict] = Field(default_factory=list)

    @field_validator("reference_time")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("reference_time requires a timezone")
        return value

    @property
    def all_resolved(self) -> bool:
        return bool(self.intents) and all(i.status == "completed" for i in self.intents.values())


class RehearsalTurn(Model):
    text: str = Field(min_length=1, max_length=2000)
    decision: TurnDecision


class RehearsalRequest(Model):
    language: Language = "en"
    turns: list[RehearsalTurn] = Field(min_length=1, max_length=20)
