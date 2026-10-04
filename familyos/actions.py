"""Doing something about a notice, without automating anybody else's service.

The alpha has an action rail, and it is deliberately the half of acting that
needs no browser, no credential belonging to a third party, and no automation
of a service that could object. Two kinds:

- **reply** -- answer the school that wrote. The commonest thing a notice asks
  for is "let us know by Friday", and answering it is an email.
- **calendar** -- a dated thing as an .ics, which a mail client offers to add.
  No calendar write scope, so no new permission from anybody.

What makes this safe enough to ship
-----------------------------------
**The recipient is derived, never supplied.** A caller sends an obligation id
and a message. The address comes from the artifact's stored `source`, computed
here. There is no parameter for it, at any layer, because a sender that accepts
an arbitrary address is an exfiltration primitive wearing a useful hat -- and
"the agent may send one email" bounds the volume, not the destination.

**The approval is bound to the exact parameters.** `params_sha256` covers
recipient, subject and body. The approving request must echo it back, and a
mismatch is refused. So an approval is an approval *of something* rather than a
flag, and nothing can change between showing and sending.

**One send per action, claimed before sending.** The same claim-then-send the
reminder sweep uses, for the same reason: at-least-once is the best anyone can
do over somebody else's network, and a duplicate should need a crash in a
window of milliseconds rather than a restart.

What is deliberately absent
---------------------------
No payment. No purchase. No browsing. The `kind` check constraint is the
deny-list, so widening it is a migration somebody writes and reviews rather
than a flag somebody flips.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import uuid

import asyncpg

from familyos import artifacts, audit, mail
from familyos.db import pool
from familyos.identity import Invalid, NotFound, Principal
from familyos.settings import settings

logger = logging.getLogger(__name__)

KINDS = ("reply", "calendar")

# A claim held this long belonged to a worker that died mid-send.
CLAIM_TTL = dt.timedelta(minutes=10)

_VISIBLE = ("SELECT o.*, a.source, a.channel, a.received_at FROM obligations o "
            "JOIN input_artifacts a ON a.id = o.artifact_id "
            f"WHERE {artifacts.VISIBLE_TO_MEMBER}")


class NothingToReplyTo(Invalid):
    """The notice carries no address we may answer, so there is nothing to
    propose. Said plainly rather than inventing a recipient."""


def fingerprint(recipient: str, subject: str, body: str) -> str:
    """What an approval is an approval of."""
    return hashlib.sha256("\x00".join((recipient, subject, body)).encode()).hexdigest()


def reply_address(source: dict, channel: str) -> str | None:
    """Who wrote the notice, if answering them is defensible.

    Mail polled from a member's own mailbox was written *to* them by the
    school, so `from` is the school and answering it is answering the sender.

    Forwarded mail is different: `from` is usually the member who forwarded it,
    and replying would mail the family back. So a forward only yields an
    address when the sender is not a member of this household -- which is the
    case where the school wrote to the household address directly.

    A WhatsApp message yields nothing. A phone number is not an address, and a
    group message was written by another parent who did not ask to be answered.
    """
    if channel not in ("gmail", "email", "email_attachment", "gmail_attachment"):
        return None
    sender = (source or {}).get("from")
    if not sender or "@" not in sender:
        return None
    return sender


async def _notice_sender_is_a_member(conn, household_id: uuid.UUID, address: str) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM members WHERE household_id = $1 AND lower(email) = lower($2)",
        household_id, address))


def _ics(obligation: dict, household_name: str) -> tuple[str, bytes]:
    """One VEVENT, by hand. iCalendar is simple enough that a dependency would
    cost more than it saves, and a hand-written line is one we can read back."""
    def esc(text: str) -> str:
        return (text or "").replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")

    def day(d) -> str:
        return d.strftime("%Y%m%d")

    start = obligation["due_date"]
    end = (obligation.get("end_date") or start) + dt.timedelta(days=1)   # DTEND is exclusive
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//FamilyOS//EN", "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{obligation['id']}@familyos",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{day(start)}",
        f"DTEND;VALUE=DATE:{day(end)}",
        f"SUMMARY:{esc(obligation['title'])}",
        f"DESCRIPTION:{esc('From a notice in ' + household_name + '. Read in FamilyOS.')}",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return (f"{obligation['id']}.ics", ("\r\n".join(lines) + "\r\n").encode())


# ----------------------------------------------------------------------------
# proposing
# ----------------------------------------------------------------------------

async def propose(p: Principal, obligation_id: uuid.UUID, kind: str, body: str | None = None) -> dict:
    """Work out what would be sent, and to whom, without sending it.

    There is no recipient parameter. That is the design, not an omission.
    """
    if kind not in KINDS:
        raise Invalid(f"kind must be one of: {', '.join(KINDS)}")
    async with pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(_VISIBLE + " AND o.id = $3", p.household_id, p.member_id, obligation_id)
        if row is None:
            raise NotFound("obligation")
        if row["superseded_at"] is not None:
            raise Invalid("a later notice replaced this, so there is nothing to answer yet")
        source = row["source"] or {}

        if kind == "reply":
            address = reply_address(source, row["channel"])
            if address and await _notice_sender_is_a_member(conn, p.household_id, address):
                # A forward from one of our own members: replying would mail
                # the family back rather than the school.
                address = None
            if not address:
                raise NothingToReplyTo(
                    "this notice carries no school address we can answer. Forwarded messages "
                    "usually name the member who forwarded them, not the school.")
            text = (body or "").strip()
            if not text:
                raise Invalid("say what to reply")
            subject = "Re: " + ((source.get("subject") or row["title"])[:180])
            recipient, send_body = address, text
        else:
            if row["due_date"] is None:
                raise Invalid("this has no date, so there is nothing to put in a calendar")
            member_email = await conn.fetchval("SELECT email FROM members WHERE id = $1", p.member_id)
            if not member_email:
                raise Invalid("you have no email address on record to send it to")
            recipient = member_email
            subject = row["title"]
            send_body = (f"{row['title']}\n\n{row['action']} by {row['due_date'].isoformat()}.\n\n"
                         "The calendar entry is attached.\n")

        digest = fingerprint(recipient, subject, send_body)
        action_id = uuid.uuid4()
        try:
            made = await conn.fetchrow(
                "INSERT INTO actions (id, household_id, obligation_id, artifact_id, kind, recipient, "
                "subject, body, params_sha256, proposed_by) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING *",
                action_id, p.household_id, obligation_id, row["artifact_id"], kind, recipient,
                subject, send_body, digest, p.member_id)
        except asyncpg.UniqueViolationError:
            raise Invalid("there is already one of these waiting to be decided") from None
        await audit.record(p.household_id, "action.proposed", actor_member_id=p.member_id,
                           target_type="action", target_id=action_id,
                           # The words are not repeated here, the same rule that
                           # keeps a notice's text out of the audit trail.
                           detail={"kind": kind, "obligation_id": str(obligation_id),
                                   "recipient_domain": recipient.rsplit("@", 1)[-1]}, conn=conn)
    return dict(made)


async def approve(p: Principal, action_id: uuid.UUID, params_sha256: str) -> dict:
    """Approve exactly what was shown.

    The caller echoes the fingerprint it displayed. A mismatch means what is
    stored is not what the person read, so the approval does not apply to it.
    """
    async with pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT a.* FROM actions a JOIN input_artifacts art ON art.id = a.artifact_id "
            f"WHERE a.id = $3 AND {artifacts.VISIBLE_TO_MEMBER.replace('a.', 'art.')} FOR UPDATE OF a",
            p.household_id, p.member_id, action_id)
        if row is None:
            raise NotFound("action")
        if row["status"] != "proposed":
            raise Invalid(f"this action is {row['status']}, not waiting to be approved")
        # Recomputed from what is stored *now*, not read back from the column.
        # Comparing the caller's echo against a stored hash proves nothing: the
        # hash was written when the action was proposed and does not change
        # when the content does. The fingerprint has to be derived again.
        current = fingerprint(row["recipient"], row["subject"], row["body"])
        if current != row["params_sha256"]:
            raise Invalid("this action has changed since it was proposed, so no approval applies "
                          "to it. Look at it again.")
        if (params_sha256 or "").lower() != current:
            raise Invalid("this is not what you were shown — look at it again.")
        done = await conn.fetchrow(
            "UPDATE actions SET status = 'approved', approved_by = $2, approved_at = NOW() "
            "WHERE id = $1 RETURNING *", action_id, p.member_id)
        await audit.record(p.household_id, "action.approved", actor_member_id=p.member_id,
                           target_type="action", target_id=action_id,
                           detail={"kind": row["kind"], "recipient_domain": row["recipient"].rsplit("@", 1)[-1]},
                           conn=conn)
    return dict(done)


async def cancel(p: Principal, action_id: uuid.UUID) -> dict:
    async with pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT a.* FROM actions a JOIN input_artifacts art ON art.id = a.artifact_id "
            f"WHERE a.id = $3 AND {artifacts.VISIBLE_TO_MEMBER.replace('a.', 'art.')} FOR UPDATE OF a",
            p.household_id, p.member_id, action_id)
        if row is None:
            raise NotFound("action")
        if row["status"] in ("sent", "sending"):
            raise Invalid("that has already gone")
        done = await conn.fetchrow(
            "UPDATE actions SET status = 'cancelled' WHERE id = $1 RETURNING *", action_id)
        await audit.record(p.household_id, "action.cancelled", actor_member_id=p.member_id,
                           target_type="action", target_id=action_id, detail={"kind": row["kind"]}, conn=conn)
    return dict(done)


async def list_for(p: Principal, limit: int = 100) -> list[dict]:
    """Actions on notices this member can see. The same rule as every read."""
    rows = await pool().fetch(
        "SELECT a.* FROM actions a JOIN input_artifacts art ON art.id = a.artifact_id "
        f"WHERE {artifacts.VISIBLE_TO_MEMBER.replace('a.', 'art.')} "
        "ORDER BY a.created_at DESC LIMIT $3", p.household_id, p.member_id, limit)
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------
# sending
# ----------------------------------------------------------------------------

_CLAIM = """
WITH due AS (
    SELECT a.id FROM actions a
    JOIN input_artifacts art ON art.id = a.artifact_id
    JOIN obligations o ON o.id = a.obligation_id
    WHERE (a.status = 'approved' OR (a.status = 'sending' AND a.claimed_at < $1::timestamptz - $3::interval))
      AND art.status = 'accepted'
      AND o.superseded_at IS NULL
    ORDER BY a.approved_at
    LIMIT $2
    FOR UPDATE OF a SKIP LOCKED
)
UPDATE actions a SET status = 'sending', claimed_at = $1::timestamptz, attempts = a.attempts + 1
FROM due WHERE a.id = due.id
RETURNING a.*
"""


async def send_approved(now: dt.datetime | None = None, limit: int = 50) -> dict[str, int]:
    """Claim, then send. A superseded obligation is not sent for: if a later
    notice changed the date, answering the old one is worse than not
    answering."""
    now = now or dt.datetime.now(dt.UTC)
    counts = {"sent": 0, "failed": 0}
    rows = await pool().fetch(_CLAIM, now, limit, CLAIM_TTL)
    for raw in rows:
        row = dict(raw)
        try:
            ics = None
            if row["kind"] == "calendar":
                async with pool().acquire() as conn:
                    ob = await conn.fetchrow(
                        "SELECT o.*, h.name AS household FROM obligations o "
                        "JOIN households h ON h.id = o.household_id WHERE o.id = $1", row["obligation_id"])
                ics = _ics(dict(ob), ob["household"])
            await mail.send(row["recipient"], row["subject"], row["body"], ics=ics)
        except Exception as exc:  # noqa: BLE001 - recorded on the row, retried next sweep
            counts["failed"] += 1
            logger.warning("action %s could not be sent", row["id"], exc_info=True)
            await pool().execute(
                "UPDATE actions SET error = $2, claimed_at = NULL, "
                "status = CASE WHEN attempts >= $3 THEN 'failed' ELSE 'approved' END WHERE id = $1",
                row["id"], f"{type(exc).__name__}: {exc}"[:500], settings.action_max_attempts)
            continue
        counts["sent"] += 1
        async with pool().acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE actions SET status = 'sent', sent_at = NOW(), error = NULL WHERE id = $1", row["id"])
            await audit.record(row["household_id"], "action.sent", actor_kind="system",
                               target_type="action", target_id=row["id"],
                               detail={"kind": row["kind"], "obligation_id": str(row["obligation_id"]),
                                       "approved_by": str(row["approved_by"]),
                                       "recipient_domain": row["recipient"].rsplit("@", 1)[-1]}, conn=conn)
    return counts


KIND = "send_actions"
HANDLER_VERSION = "1"


async def _handle(job: dict, ctx) -> dict:
    now = dt.datetime.now(dt.UTC)
    await ctx.plan(["Send what has been approved"])
    await ctx.step("0")
    counts = await send_approved(now)
    await ctx.finish_step("0")
    every = max(30, settings.action_sweep_seconds)
    return {**counts, "next_run_at": (now + dt.timedelta(seconds=every)).isoformat()}


async def ensure_scheduled() -> dict | None:
    """One sweep for the whole deployment, like the reminders. Off unless
    actions are enabled and there is a way to send."""
    from familyos import jobs
    if not (settings.actions_enabled and mail.configured()):
        return None
    existing = await pool().fetchrow(
        "SELECT id FROM jobs WHERE kind = $1 AND status NOT IN ('succeeded', 'failed', 'cancelled') LIMIT 1", KIND)
    if existing is not None:
        return None
    return await jobs.create(KIND, {}, next_run_at=dt.datetime.now(dt.UTC))


def register() -> None:
    from familyos import jobs
    jobs.register(KIND, _handle, version=HANDLER_VERSION,
                  settings_keys=("action_sweep_seconds", "action_max_attempts"))


def describe() -> str:
    if not settings.actions_enabled:
        return "Actions are off (FAMILYOS_ACTIONS_ENABLED=false): nothing is sent on a family's behalf."
    if not mail.configured():
        return ("Actions are MISCONFIGURED: enabled, but FAMILYOS_SMTP_HOST is not set, so an "
                "approved reply can never leave.")
    return ("Actions are on: reply and calendar only, recipient derived from the notice, "
            "every send approved against the exact words.")


__all__ = ["KIND", "KINDS", "approve", "cancel", "describe", "ensure_scheduled", "fingerprint",
           "list_for", "propose", "register", "reply_address", "send_approved"]
