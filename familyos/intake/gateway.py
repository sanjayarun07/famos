"""The intake and trust gateway: every channel lands here.

Four steps, in order, for every item:

  1. Receive:  the channel adapter turns what arrived into an InboundEnvelope.
  2. Identify: who sent it? A signed-in member, or an email sender matched
               to a member and authenticated by the receiving provider.
  3. Decide:   accept, quarantine (a guardian must vouch for it) or reject
               (nothing is stored).
  4. Scope:    who may see it. Everything starts private to its sender
               unless the sender chose shared.

Only then is the original stored. No model runs before this point, and
nothing downstream ever sees an item that was not accepted.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from familyos import artifacts, audit
from familyos.db import pool
from familyos.identity import Principal, household_by_inbound_token, member_by_email
from familyos.intake import email as email_adapter
from familyos.intake.sniff import ALLOWED, sniff
from familyos.settings import settings


class Rejected(Exception):
    def __init__(self, reason: str, status: int = 422):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class InboundEnvelope:
    channel: str
    data: bytes
    media_type: str
    filename: str | None = None
    source: dict = field(default_factory=dict)
    note: str | None = None


# ----------------------------------------------------------------------------
# share sheet / upload
# ----------------------------------------------------------------------------

async def receive_upload(p: Principal, data: bytes, *, filename: str | None, declared_type: str | None,
                         visibility: str = "private", subject_member_ids: list[uuid.UUID] | None = None,
                         note: str | None = None) -> tuple[dict, bool]:
    envelope = InboundEnvelope(channel="upload", data=data, media_type=sniff(data, declared_type),
                               filename=_clean_filename(filename), note=note)
    if not data:
        await _rejected(p.household_id, p, "empty")
    if len(data) > settings.max_upload_bytes:
        await _rejected(p.household_id, p, "too_large", status=413)
    if envelope.media_type not in ALLOWED:
        await _rejected(p.household_id, p, "unsupported_type", status=415)
    digest = artifacts.sha256(data)
    async with pool().acquire() as conn, conn.transaction():
        return await artifacts.create(conn, p.household_id, artifacts.NewArtifact(
            data=data, media_type=envelope.media_type, channel="upload", visibility=visibility, status="accepted",
            submitted_by=p.member_id, filename=envelope.filename, note=note,
            # The same member sending the same bytes again gets the same artifact.
            dedup_key=f"upload:{p.member_id}:{digest}",
            subject_member_ids=tuple(subject_member_ids or ())))


# ----------------------------------------------------------------------------
# forwarding email
# ----------------------------------------------------------------------------

async def receive_email(raw: bytes, recipient: str | None = None) -> tuple[dict, bool]:
    """A raw RFC 822 message posted by the mail provider. `recipient` is the
    SMTP envelope recipient when the provider supplies it; otherwise the
    household is found from the To/Cc/Delivered-To headers."""
    if len(raw) > settings.max_email_bytes:
        raise Rejected("too_large", status=413)
    message = email_adapter.parse(raw)
    household = None
    for address in ([recipient] if recipient else []) + message.recipients:
        token = email_adapter.inbound_token(address, settings.inbound_domain)
        if token and (household := await household_by_inbound_token(token)):
            break
    if household is None:
        # Unknown address: no household to hold it, so nothing is kept.
        raise Rejected("unknown_recipient", status=404)
    household_id = household["id"]

    member = await member_by_email(household_id, message.sender) if message.sender else None
    authenticated = message.sender_authenticated or not settings.require_sender_auth
    if member is None:
        status, reason = "quarantined", "unknown_sender"
    elif not authenticated:
        status, reason = "quarantined", "sender_not_authenticated"
    else:
        status, reason = "accepted", None
    submitted_by = member["id"] if member and status == "accepted" else None

    source = {"from": message.sender, "to": message.recipients, "subject": message.subject,
              "message_id": message.message_id, "date": message.date, "sender_authenticated": message.sender_authenticated,
              "skipped_attachments": [{"media_type": a.media_type, "size_bytes": len(a.data)}
                                      for a in message.attachments if sniff(a.data, a.media_type) not in ALLOWED]}
    dedup_key = f"email:{message.message_id}" if message.message_id else f"email-sha:{artifacts.sha256(raw)}"
    async with pool().acquire() as conn, conn.transaction():
        parent, duplicate = await artifacts.create(conn, household_id, artifacts.NewArtifact(
            data=raw, media_type="message/rfc822", channel="email", visibility="private", status=status,
            submitted_by=submitted_by, source=source, dedup_key=dedup_key, quarantine_reason=reason),
            actor_kind="inbound")
        if duplicate:
            return parent, True
        for attachment in message.attachments:
            media_type = sniff(attachment.data, attachment.media_type)
            if media_type not in ALLOWED or not attachment.data:
                continue
            await artifacts.create(conn, household_id, artifacts.NewArtifact(
                data=attachment.data, media_type=media_type, channel="email_attachment", visibility="private",
                status=status, submitted_by=submitted_by, filename=_clean_filename(attachment.filename),
                parent_id=parent["id"], quarantine_reason=reason), actor_kind="inbound")
        parent = dict(await conn.fetchrow(artifacts._SELECT + " WHERE a.id = $1", parent["id"]))
    return parent, False


# ----------------------------------------------------------------------------

async def _rejected(household_id: uuid.UUID, p: Principal | None, reason: str, status: int = 422):
    await audit.record(household_id, "intake.rejected", actor_member_id=p.member_id if p else None,
                       actor_kind="member" if p else "inbound", detail={"reason": reason})
    raise Rejected(reason, status)


def _clean_filename(name: str | None) -> str | None:
    if not name:
        return None
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:255] or None
