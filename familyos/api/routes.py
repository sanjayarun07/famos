"""HTTP API, v1."""
from __future__ import annotations

import hmac
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile, status

from familyos import artifacts, audit, consent, erasure, identity
from familyos.api.deps import current_member
from familyos.identity import Principal
from familyos.intake import gateway
from familyos.models import (
    Artifact,
    AuditEvent,
    Consent,
    ConsentIn,
    Credentials,
    Erasure,
    Household,
    HouseholdCreated,
    HouseholdEraseIn,
    HouseholdIn,
    IntakeResult,
    Member,
    MemberIn,
    QuarantineAcceptIn,
    Visibility,
    VisibilityIn,
)
from familyos.settings import settings

router = APIRouter(prefix="/v1")


def _household(row: dict) -> Household:
    return Household(id=row["id"], name=row["name"], inbound_address=identity.inbound_address(row["inbound_token"]),
                     status=row["status"], created_at=row["created_at"],
                     members=[Member(**m) for m in row.get("members", [])])


def _member(row: dict) -> Member:
    return Member(**{k: row[k] for k in Member.model_fields if k in row})


def _artifact(row: dict) -> Artifact:
    return Artifact(**{k: row[k] for k in Artifact.model_fields if k in row})


# ----------------------------------------------------------------------------
# households and members
# ----------------------------------------------------------------------------

@router.post("/households", response_model=HouseholdCreated, status_code=201, tags=["household"])
async def create_household(body: HouseholdIn):
    household, member, token = await identity.create_household(body)
    full = await identity.get_household(household["id"])
    return HouseholdCreated(household=_household(full), credentials=Credentials(member=_member(member), token=token))


@router.get("/me", response_model=Member, tags=["household"])
async def me(p: Principal = Depends(current_member)):
    return _member(await identity.get_member(p.household_id, p.member_id))


@router.get("/household", response_model=Household, tags=["household"])
async def get_household(p: Principal = Depends(current_member)):
    return _household(await identity.get_household(p.household_id))


@router.post("/household/members", status_code=201, tags=["household"],
             response_model=Credentials | Member,
             description="Guardians add members. Guardians and adults receive a sign-in token once; children do not sign in.")
async def add_member(body: MemberIn, p: Principal = Depends(current_member)):
    member, token = await identity.add_member(p, body)
    return Credentials(member=_member(member), token=token) if token else _member(member)


@router.post("/household/erase", response_model=Erasure, status_code=202, tags=["erasure"],
             description="Erase the whole household. Sign-in stops at once; the household key is destroyed, then every "
                         "original, record and audit event is deleted.")
async def erase_household(body: HouseholdEraseIn, p: Principal = Depends(current_member)):
    return Erasure(**await erasure.request_household_erasure(p, body.confirm_name))


@router.post("/members/{member_id}/erase", response_model=Erasure, status_code=202, tags=["erasure"])
async def erase_member(member_id: uuid.UUID, p: Principal = Depends(current_member)):
    return Erasure(**await erasure.request_subject_erasure(p, member_id, reason="member_erased", delete_member=True))


@router.get("/erasures/{erasure_id}", response_model=Erasure, tags=["erasure"])
async def get_erasure(erasure_id: uuid.UUID, p: Principal = Depends(current_member)):
    return Erasure(**await erasure.get(p.household_id, erasure_id))


# ----------------------------------------------------------------------------
# artifacts
# ----------------------------------------------------------------------------

@router.post("/artifacts", response_model=IntakeResult, tags=["intake"],
             responses={201: {"model": IntakeResult}, 200: {"description": "Duplicate: already received"}})
async def upload(response: Response, file: UploadFile = File(...), visibility: Visibility = Form("private"),
                 subject_member_ids: list[uuid.UUID] = Form(default_factory=list), note: str | None = Form(None),
                 p: Principal = Depends(current_member)):
    data = await file.read(settings.max_upload_bytes + 1)
    row, duplicate = await gateway.receive_upload(p, data, filename=file.filename, declared_type=file.content_type,
                                                  visibility=visibility, subject_member_ids=subject_member_ids, note=note)
    response.status_code = 200 if duplicate else 201
    return IntakeResult(duplicate=duplicate, artifact=_artifact(row))


@router.get("/artifacts", response_model=list[Artifact], tags=["artifacts"])
async def list_artifacts(limit: int = 50, p: Principal = Depends(current_member)):
    return [_artifact(r) for r in await artifacts.list_visible(p, limit=min(max(limit, 1), 200))]


@router.get("/artifacts/{artifact_id}", response_model=Artifact, tags=["artifacts"])
async def get_artifact(artifact_id: uuid.UUID, p: Principal = Depends(current_member)):
    return _artifact(await artifacts.get_visible(p, artifact_id))


@router.get("/artifacts/{artifact_id}/original", tags=["artifacts"], response_class=Response)
async def get_original(artifact_id: uuid.UUID, p: Principal = Depends(current_member)):
    row, data = await artifacts.read_original(p, artifact_id)
    name = row["filename"] or f"{row['id']}{'.eml' if row['media_type'] == 'message/rfc822' else ''}"
    return Response(content=data, media_type=row["media_type"], headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    })


@router.patch("/artifacts/{artifact_id}", response_model=Artifact, tags=["artifacts"])
async def set_visibility(artifact_id: uuid.UUID, body: VisibilityIn, p: Principal = Depends(current_member)):
    return _artifact(await artifacts.set_visibility(p, artifact_id, body.visibility))


@router.put("/artifacts/{artifact_id}/subjects", response_model=Artifact, tags=["artifacts"])
async def set_subjects(artifact_id: uuid.UUID, member_ids: list[uuid.UUID], p: Principal = Depends(current_member)):
    return _artifact(await artifacts.set_subjects(p, artifact_id, member_ids))


@router.delete("/artifacts/{artifact_id}", status_code=204, tags=["artifacts"])
async def delete_artifact(artifact_id: uuid.UUID, p: Principal = Depends(current_member)):
    await artifacts.delete_by_member(p, artifact_id)


# ----------------------------------------------------------------------------
# quarantine
# ----------------------------------------------------------------------------

@router.get("/quarantine", response_model=list[Artifact], tags=["intake"])
async def list_quarantine(p: Principal = Depends(current_member)):
    return [_artifact(r) for r in await artifacts.list_quarantine(p)]


@router.post("/quarantine/{artifact_id}/accept", response_model=Artifact, tags=["intake"])
async def accept_quarantined(artifact_id: uuid.UUID, body: QuarantineAcceptIn, p: Principal = Depends(current_member)):
    return _artifact(await artifacts.accept_quarantined(p, artifact_id, body.member_id, body.visibility))


@router.post("/quarantine/{artifact_id}/reject", status_code=204, tags=["intake"])
async def reject_quarantined(artifact_id: uuid.UUID, p: Principal = Depends(current_member)):
    await artifacts.reject_quarantined(p, artifact_id)


# ----------------------------------------------------------------------------
# forwarding email (called by the mail provider)
# ----------------------------------------------------------------------------

@router.post("/inbound/email", tags=["intake"], status_code=202,
             description="The mail provider posts the raw RFC 822 message as the request body, with the shared secret "
                         "in X-FamilyOS-Webhook-Secret and the SMTP envelope recipient in X-FamilyOS-Recipient.")
async def inbound_email(request: Request, x_familyos_webhook_secret: str = Header(""),
                        x_familyos_recipient: str | None = Header(None)):
    if not settings.inbound_webhook_secret or not hmac.compare_digest(
            x_familyos_webhook_secret.encode(), settings.inbound_webhook_secret.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook secret")
    raw = await request.body()
    row, duplicate = await gateway.receive_email(raw, recipient=x_familyos_recipient)
    # The sender is not told whether their mail was accepted or quarantined.
    return {"received": True, "duplicate": duplicate, "status": row["status"], "id": str(row["id"])}


# ----------------------------------------------------------------------------
# consent and audit
# ----------------------------------------------------------------------------

@router.post("/consents", response_model=Consent, status_code=201, tags=["consent"])
async def grant_consent(body: ConsentIn, p: Principal = Depends(current_member)):
    return Consent(**await consent.grant(p, body))


@router.get("/consents", response_model=list[Consent], tags=["consent"])
async def list_consents(p: Principal = Depends(current_member)):
    return [Consent(**r) for r in await consent.list_for(p.household_id)]


@router.post("/consents/{consent_id}/withdraw", response_model=Erasure, status_code=202, tags=["consent"],
             description="Withdraw consent. Everything that names the child is erased; the child stays a member.")
async def withdraw_consent(consent_id: uuid.UUID, p: Principal = Depends(current_member)):
    return Erasure(**await erasure.withdraw_consent(p, consent_id))


@router.get("/audit", response_model=list[AuditEvent], tags=["audit"])
async def list_audit(limit: int = 100, before_id: int | None = None, p: Principal = Depends(current_member)):
    if not p.is_guardian:
        raise identity.NotAllowed("only a guardian can read the audit trail")
    return [AuditEvent(**r) for r in await audit.list_for(p.household_id, min(max(limit, 1), 500), before_id)]
