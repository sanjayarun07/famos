"""API schemas."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

Role = Literal["guardian", "adult", "child"]
Visibility = Literal["private", "shared"]


class MemberIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    role: Role
    email: EmailStr | None = None
    date_of_birth: date | None = None


class GuardianIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    email: EmailStr


class HouseholdIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    guardian: GuardianIn


class Member(BaseModel):
    id: uuid.UUID
    household_id: uuid.UUID
    display_name: str
    role: Role
    email: str | None = None
    date_of_birth: date | None = None
    created_at: datetime


class Household(BaseModel):
    id: uuid.UUID
    name: str
    inbound_address: str
    status: str
    created_at: datetime
    members: list[Member] = []


class Credentials(BaseModel):
    """Returned once; only its hash is stored."""
    member: Member
    token: str


class HouseholdCreated(BaseModel):
    household: Household
    credentials: Credentials


class Artifact(BaseModel):
    id: uuid.UUID
    household_id: uuid.UUID
    parent_id: uuid.UUID | None
    channel: str
    submitted_by: uuid.UUID | None
    visibility: Visibility
    status: str
    quarantine_reason: str | None
    filename: str | None
    media_type: str
    size_bytes: int
    sha256: str
    source: dict
    note: str | None
    subject_member_ids: list[uuid.UUID] = []
    children: list[uuid.UUID] = []
    received_at: datetime
    accepted_at: datetime | None


class IntakeResult(BaseModel):
    """`duplicate` is true when these exact bytes had already been sent by
    the same member (or the same email delivered twice): nothing new was
    stored and `artifact` is the one made the first time."""
    duplicate: bool
    artifact: Artifact


class VisibilityIn(BaseModel):
    visibility: Visibility


class QuarantineAcceptIn(BaseModel):
    member_id: uuid.UUID
    visibility: Visibility = "private"


class ConsentIn(BaseModel):
    subject_member_id: uuid.UUID
    purpose: str = Field(default="household_records", min_length=1, max_length=64)
    notice_version: str = Field(min_length=1, max_length=32)
    verification_method: str = Field(min_length=1, max_length=64,
                                     description="How the guardian was verified, e.g. 'account_holder' or 'digilocker'.")


class Consent(BaseModel):
    id: uuid.UUID
    household_id: uuid.UUID
    subject_member_id: uuid.UUID
    guardian_member_id: uuid.UUID | None
    purpose: str
    notice_version: str
    verification_method: str
    granted_at: datetime
    withdrawn_at: datetime | None
    withdrawn_by: uuid.UUID | None


class HouseholdEraseIn(BaseModel):
    confirm_name: str = Field(description="The household's name, typed to confirm.")


class Erasure(BaseModel):
    id: uuid.UUID
    scope: str
    status: str
    requested_at: datetime
    completed_at: datetime | None
    counts: dict


class AuditEvent(BaseModel):
    id: int
    actor_kind: str
    actor_member_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: uuid.UUID | None
    detail: dict
    at: datetime
