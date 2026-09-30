"""Reminders: telling the family before the date passes.

Intake stores a notice, extraction reads obligations out of it, and until now
that was the end of it: a dated thing sat in a list and nobody was told. This
module closes that gap, as one recurring job on `jobs.py` (whose `next_run_at`
has been there, unused, since it came over from Orbit).

Two reasons to be reminded:

- **due** -- an accepted obligation is coming up. The family agreed to it.
- **undecided** -- a proposal nobody has accepted or dismissed, and its date is
  near. This is the one that actually gets missed, so it is worth one nudge.

Who is told follows the same visibility rule as every read: a shared notice
reminds the adults and guardians of the household, a private one reminds only
the member who sent it. Never a child; they do not sign in, and an obligation's
subject is who it is *about*, not who is told.

Sending is idempotent by construction. One row per (obligation, person, reason,
lead time) with a unique index, so the sweep can run as often as it likes: a
row that exists is not made again, and a row already `sent` is not sent again.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from familyos import audit, jobs
from familyos.db import pool
from familyos.identity import Principal
from familyos.settings import settings

logger = logging.getLogger(__name__)

KIND = "send_reminders"
HANDLER_VERSION = "1"


def lead_days() -> list[int]:
    """Days before the date to remind on, nearest last so the first row made
    is the earliest one."""
    out = []
    for piece in (settings.reminder_lead_days or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            value = int(piece)
        except ValueError:
            continue
        if value >= 0 and value not in out:
            out.append(value)
    return sorted(out, reverse=True) or [0]


# ----------------------------------------------------------------------------
# scheduling
# ----------------------------------------------------------------------------

# One row per person who may see the obligation, at one lead time. The
# visibility test is artifacts.VISIBLE_TO_MEMBER written against the join:
# a member sees an accepted artifact when it is shared, or they submitted it.
_SCHEDULE = """
INSERT INTO reminders (id, household_id, obligation_id, member_id, reason, lead_days, send_after, channel)
SELECT gen_random_uuid(), o.household_id, o.id, m.id, $1, $2,
       (o.due_date::timestamptz - make_interval(days => $2)), $3
FROM obligations o
JOIN input_artifacts a ON a.id = o.artifact_id
JOIN members m ON m.household_id = o.household_id AND m.role <> 'child'
WHERE o.due_date IS NOT NULL
  AND o.status = $4
  AND a.status = 'accepted'
  AND (a.visibility = 'shared' OR a.submitted_by = m.id)
  AND o.due_date >= $5::date
ON CONFLICT (obligation_id, member_id, reason, lead_days) DO NOTHING
RETURNING id
"""


async def schedule(conn, today: dt.date) -> dict[str, int]:
    """Make the reminder rows that do not exist yet. Safe to repeat."""
    made = {"due": 0, "undecided": 0}
    for lead in lead_days():
        for reason, status in (("due", "accepted"), ("undecided", "proposed")):
            rows = await conn.fetch(_SCHEDULE, reason, lead, settings.reminder_channel, status, today)
            made[reason] += len(rows)
    # A proposal that has since been decided should not nudge, and neither
    # should anything whose obligation was dismissed.
    cancelled = await conn.fetch(
        "UPDATE reminders r SET status = 'cancelled' FROM obligations o "
        "WHERE o.id = r.obligation_id AND r.status = 'pending' AND ("
        "  (r.reason = 'undecided' AND o.status <> 'proposed') OR"
        "  (r.reason = 'due' AND o.status <> 'accepted')) RETURNING r.id")
    made["cancelled"] = len(cancelled)
    return made


# ----------------------------------------------------------------------------
# delivery
# ----------------------------------------------------------------------------

_DUE_WORDS = {
    "sign": "needs signing",
    "pay": "needs paying",
    "attend": "needs someone there",
    "submit": "needs submitting",
    "prepare": "needs getting ready",
    "note": "is coming up",
}


def compose(row: dict) -> tuple[str, str]:
    """Subject and body. The notice's own words are not repeated here: a
    reminder says what and when, and points at the record."""
    when = row["due_date"]
    days = (when - dt.date.today()).days if when else None
    if days is None:
        timing = ""
    elif days < 0:
        timing = "was due " + when.isoformat()
    elif days == 0:
        timing = "is due today"
    elif days == 1:
        timing = "is due tomorrow"
    else:
        timing = "is due in " + str(days) + " days, on " + when.isoformat()

    if row["reason"] == "undecided":
        subject = "Still to decide: " + row["title"]
        body = ("Nobody has accepted or dismissed this yet, and it " + timing + ".\n\n"
                + row["title"] + "\n")
    else:
        subject = row["title"] + " " + (_DUE_WORDS.get(row["action"]) or "is coming up")
        body = row["title"] + " " + timing + ".\n\n"

    if row.get("subject_name"):
        body += "For: " + row["subject_name"] + "\n"
    body += "From a notice received " + row["received_at"].date().isoformat() + ".\n"
    return subject, body


async def _send_log(row: dict, subject: str, body: str) -> None:
    logger.info("reminder %s for member %s: %s", row["id"], row["member_id"], subject)


async def _send_email(row: dict, subject: str, body: str) -> None:
    if not settings.smtp_host:
        raise RuntimeError("FAMILYOS_SMTP_HOST is not set, so the email channel cannot send")
    if not row.get("email"):
        raise RuntimeError("that member has no email address")

    def deliver() -> None:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = settings.smtp_from or settings.smtp_username
        msg["To"] = row["email"]
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            if settings.smtp_starttls:
                server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)

    await asyncio.to_thread(deliver)


CHANNELS = {"log": _send_log, "email": _send_email}

_DUE = """
SELECT r.*, o.title, o.action, o.due_date, o.subject_member_id, m.email, m.display_name,
       a.received_at, c.subject_name
FROM reminders r
JOIN obligations o ON o.id = r.obligation_id
JOIN members m ON m.id = r.member_id
JOIN input_artifacts a ON a.id = o.artifact_id
LEFT JOIN claims c ON c.id = o.claim_id
WHERE r.status = 'pending' AND r.send_after <= $1
ORDER BY r.send_after
LIMIT $2
"""


async def send_due(now: dt.datetime, limit: int = 200) -> dict[str, int]:
    counts = {"sent": 0, "failed": 0}
    rows = await pool().fetch(_DUE, now, limit)
    for raw in rows:
        row = dict(raw)
        send = CHANNELS.get(row["channel"]) or _send_log
        try:
            subject, body = compose(row)
            await send(row, subject, body)
        except Exception as exc:  # noqa: BLE001 - recorded on the row, retried next sweep
            counts["failed"] += 1
            logger.warning("reminder %s could not be sent", row["id"], exc_info=True)
            await pool().execute(
                "UPDATE reminders SET attempts = attempts + 1, error = $2, "
                "status = CASE WHEN attempts + 1 >= $3 THEN 'failed' ELSE 'pending' END WHERE id = $1",
                row["id"], f"{type(exc).__name__}: {exc}"[:500], settings.reminder_max_attempts)
            continue
        counts["sent"] += 1
        async with pool().acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE reminders SET status = 'sent', sent_at = NOW(), attempts = attempts + 1, error = NULL WHERE id = $1",
                row["id"])
            # Counts and ids, never the notice's words.
            await audit.record(row["household_id"], "reminder.sent", actor_kind="system",
                               target_type="obligation", target_id=row["obligation_id"],
                               detail={"reminder_id": str(row["id"]), "member_id": str(row["member_id"]),
                                       "reason": row["reason"], "lead_days": row["lead_days"],
                                       "channel": row["channel"]}, conn=conn)
    return counts


# ----------------------------------------------------------------------------
# the recurring job
# ----------------------------------------------------------------------------

async def _handle(job: dict, ctx: jobs.JobContext) -> dict:
    now = dt.datetime.now(dt.UTC)
    await ctx.plan(["Work out what needs remembering", "Tell whoever may see it"])

    await ctx.step("0")
    async with pool().acquire() as conn, conn.transaction():
        made = await schedule(conn, now.date())
    await ctx.finish_step("0")

    await ctx.step("1")
    sent = await send_due(now)
    await ctx.finish_step("1")

    every = max(60.0, settings.reminder_sweep_seconds)
    return {"scheduled": made, **sent, "next_run_at": (now + dt.timedelta(seconds=every)).isoformat()}


async def ensure_scheduled() -> dict | None:
    """One sweep job for the whole deployment. A system job: it belongs to no
    household, so it outlives any of them."""
    if not settings.reminders_enabled:
        return None
    existing = await pool().fetchrow(
        "SELECT id FROM jobs WHERE kind = $1 AND status NOT IN ('succeeded', 'failed', 'cancelled') LIMIT 1", KIND)
    if existing is not None:
        return None
    return await jobs.create(KIND, {}, next_run_at=dt.datetime.now(dt.UTC))


# ----------------------------------------------------------------------------
# reading
# ----------------------------------------------------------------------------

async def list_for(p: Principal, limit: int = 100) -> list[dict]:
    """What this member is going to be told, and was told. A member only ever
    has rows for obligations they can see, so no extra filter is needed."""
    rows = await pool().fetch(
        "SELECT r.id, r.obligation_id, r.reason, r.lead_days, r.send_after, r.channel, r.status, r.sent_at, "
        "o.title, o.action, o.due_date, o.subject_member_id "
        "FROM reminders r JOIN obligations o ON o.id = r.obligation_id "
        "WHERE r.member_id = $1 AND r.household_id = $2 ORDER BY r.send_after LIMIT $3",
        p.member_id, p.household_id, limit)
    return [dict(r) for r in rows]


def register() -> None:
    jobs.register(KIND, _handle, version=HANDLER_VERSION,
                  settings_keys=("reminder_lead_days", "reminder_channel", "reminder_sweep_seconds"))
