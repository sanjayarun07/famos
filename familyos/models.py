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
    # The number they forward from on WhatsApp. Any shape a person types;
    # stored as digits.
    phone: str | None = Field(default=None, max_length=24)
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
    phone: str | None = None
    date_of_birth: date | None = None
    created_at: datetime


class Household(BaseModel):
    id: uuid.UUID
    name: str
    inbound_address: str
    # Null until a business number is configured: the console only offers a way
    # in that actually works.
    whatsapp_number: str | None = None
    status: str
    created_at: datetime
    members: list[Member] = []


class Session(BaseModel):
    """A live sign-in. The token itself is never shown again."""
    id: uuid.UUID
    member_id: uuid.UUID
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    current: bool


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


# ----------------------------------------------------------------------------
# extraction
# ----------------------------------------------------------------------------

class SourceLocation(BaseModel):
    """Where a claim's quote sits in the original: the page, the character
    span in that page's extracted text, and one box per line (PDF points
    from the top left, or image pixels)."""
    page: int | None
    span_start: int | None
    span_end: int | None
    boxes: list[list[float]] | None
    match: str | None = Field(description="exact, normalized or fuzzy; null when the quote was not found")


class ClaimOut(BaseModel):
    id: uuid.UUID
    kind: str
    title: str
    date: date | None
    end_date: date | None
    date_text: str | None
    time_text: str | None
    place: str | None
    amount: float | None
    currency: str | None
    amount_text: str | None
    applies_to: str | None
    subject_name: str | None
    requires: list[str]
    optional: bool
    uncertain: bool
    amends: str | None
    change: str | None
    quote: str
    location: SourceLocation
    confidence: float


class ExtractionOut(BaseModel):
    id: uuid.UUID
    extractor: str
    model: str | None
    prompt_version: str
    parser_version: str
    reference_date: date | None
    actionable: bool
    non_actionable_reason: str | None
    page_count: int
    ocr_pages: int
    notes: list[str]
    created_at: datetime
    claims: list[ClaimOut]


class ArtifactExtraction(BaseModel):
    """`extraction` is null until the job has finished; `job_status` says where it is."""
    artifact_id: uuid.UUID
    extraction: ExtractionOut | None
    job_id: uuid.UUID | None
    job_status: str | None
    job_error: str | None


class Amendment(BaseModel):
    """A proposed link: this notice revises that earlier one. Proposed until a
    member confirms it, because a wrong link would stop a live reminder."""
    id: uuid.UUID
    artifact_id: uuid.UUID
    claim_id: uuid.UUID
    claim_title: str
    change: str | None
    amends_artifact_id: uuid.UUID
    amends_claim_id: uuid.UUID
    amends_title: str
    amends_date: date | None
    score: float
    matched_on: str
    status: Literal["proposed", "confirmed", "rejected"]
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime


class AmendmentDecisionIn(BaseModel):
    status: Literal["confirmed", "rejected"]


class Reminder(BaseModel):
    id: uuid.UUID
    obligation_id: uuid.UUID
    reason: Literal["due", "undecided"]
    lead_days: int
    send_after: datetime
    channel: str
    status: Literal["pending", "sent", "failed", "cancelled"]
    sent_at: datetime | None
    title: str
    action: str
    due_date: date | None
    subject_member_id: uuid.UUID | None


class Mailbox(BaseModel):
    """A connected Google mailbox. There is no field here for the token, and
    no endpoint that returns one."""
    id: uuid.UUID
    email: str
    scopes: list[str]
    connected_at: datetime
    last_polled_at: datetime | None = None
    needs_reconnect: bool = False
    last_error: str | None = None
    backfill_done: bool = False
    disconnected_at: datetime | None = None


class MailboxAuthorization(BaseModel):
    """Where to send the member, and the state that ties the callback back to
    this request."""
    url: str
    state: str


LivedWhere = Literal["email", "whatsapp_group", "whatsapp_direct", "school_portal", "school_app",
                     "other_app", "sms", "paper", "word_of_mouth", "unknown"]


class GapIn(BaseModel):
    """A member saying a notice never reached them here."""
    title: str = Field(min_length=1, max_length=300)
    lived_where: LivedWhere
    # Unknown is different from no: if it was also emailed, the gap is a
    # matching problem rather than a channel problem.
    also_emailed: bool | None = None
    had_date: bool = False
    noticed_on: date | None = None
    note: str | None = Field(default=None, max_length=2000)


class GapResolveIn(BaseModel):
    """The notice that eventually arrived, or nothing to unset it."""
    arrived_as: uuid.UUID | None = None


class Gap(BaseModel):
    id: uuid.UUID
    reported_by: uuid.UUID
    title: str
    lived_where: LivedWhere
    also_emailed: bool | None = None
    had_date: bool
    noticed_on: date | None = None
    arrived_as: uuid.UUID | None = None
    note: str | None = None
    created_at: datetime


class GapWhere(BaseModel):
    lived_where: LivedWhere
    count: int
    with_a_date: int
    also_emailed: int
    arrived_later: int
    # What this answer would mean doing, carried with the number so the
    # finding and its consequence are not separated.
    means: str


class CaptureSummary(BaseModel):
    since: date
    captured: int
    missed_reported: int
    # None, not 1.0, when nothing has arrived yet.
    capture_rate: float | None = None
    captured_by_channel: dict[str, int]
    missed_by_where: list[GapWhere]
    caveat: str


ActionKind = Literal["reply", "calendar"]


class ActionIn(BaseModel):
    """What to do about an obligation. There is no recipient field: the address
    is derived from the notice, which is what stops this being a way to send
    mail anywhere."""
    kind: ActionKind
    # Required for a reply, ignored for a calendar entry.
    body: str | None = Field(default=None, max_length=4000)


class ActionApproveIn(BaseModel):
    """The fingerprint of exactly what was shown. A mismatch means the stored
    action is not what the person read, so the approval does not apply."""
    params_sha256: str = Field(min_length=64, max_length=64)


class Action(BaseModel):
    id: uuid.UUID
    obligation_id: uuid.UUID
    artifact_id: uuid.UUID
    kind: ActionKind
    recipient: str
    subject: str
    body: str
    params_sha256: str
    status: Literal["proposed", "approved", "sending", "sent", "failed", "cancelled"]
    proposed_by: uuid.UUID
    approved_by: uuid.UUID | None = None
    approved_at: datetime | None = None
    sent_at: datetime | None = None
    attempts: int
    error: str | None = None
    created_at: datetime


BriefReason = Literal["missed", "mailbox", "overdue", "today", "undecided", "soon", "amendment",
                      "quarantine"]


class BriefItem(BaseModel):
    """One line of the brief. Which ids are set depends on the reason: a dated
    thing points at its obligation, a revision at the amendment to confirm, a
    quarantined item at the notice a guardian has to vouch for."""
    reason: BriefReason
    rank: int
    title: str
    why: str
    when: str | None = None
    due_date: date | None = None
    due_time: str | None = None
    action: str | None = None
    status: str | None = None
    optional: bool = False
    # A later notice may have changed this and nobody has confirmed it, so the
    # date shown might not be the date that holds.
    contested: bool = False
    subject_member_id: uuid.UUID | None = None
    subject_name: str | None = None
    obligation_id: uuid.UUID | None = None
    amendment_id: uuid.UUID | None = None
    artifact_id: uuid.UUID | None = None
    mailbox_id: uuid.UUID | None = None


class Brief(BaseModel):
    date: date
    member_id: uuid.UUID
    display_name: str
    items: list[BriefItem]
    # Everything found, by reason -- not everything shown, so "and 4 more" can
    # be said honestly.
    counts: dict[str, int]
    more: int


class Obligation(BaseModel):
    id: uuid.UUID
    artifact_id: uuid.UUID
    claim_id: uuid.UUID
    subject_member_id: uuid.UUID | None
    kind: Literal["task", "calendar"]
    action: str
    title: str
    due_date: date | None
    end_date: date | None
    due_time: str | None
    optional: bool
    status: Literal["proposed", "accepted", "dismissed"]
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime
    # Set when a later notice replaced the claim this came from. The row stays,
    # so "what happened to that?" has an answer.
    superseded_at: datetime | None = None
    superseded_by_artifact_id: uuid.UUID | None = None


class ObligationDecisionIn(BaseModel):
    status: Literal["accepted", "dismissed"]
