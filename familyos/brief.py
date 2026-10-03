"""The daily brief: the few things that matter today, for one member.

The rest of the system answers *what is there* -- notices, claims, obligations,
reminders, amendments. A family does not want a list. It wants to be told the
two or three things that go wrong if nobody moves today. That is this.

It is a projection and deliberately not a table. Nothing here is stored: the
brief is derived from obligations, amendments and quarantine every time it is
asked for. So it needs no erasure path of its own -- destroy the household key
and there is nothing left to compute it from -- and it cannot drift out of step
with the records it describes. A stored brief would be a small twin, with all
the same problems as a large one.

What counts as mattering, worst first:

- **missed** -- dated, nobody decided, and the date has gone. The exact failure
  this product exists to prevent, so it leads.
- **mailbox** -- a connected mailbox has stopped being readable and needs
  reconnecting. Second, because it is the only line here about something the
  family cannot see: everything else in the brief is only as complete as
  intake, and a dead mailbox means notices are arriving nowhere. Worth saying
  loudly -- a Google client still in testing issues refresh tokens that expire
  in seven days, so this is a weekly event until CASA clears.
- **overdue** -- the family accepted it and the date has gone.
- **today** -- whatever falls today, decided or not.
- **undecided** -- a proposal with a date coming up. The one that gets missed.
- **soon** -- accepted and coming up.
- **amendment** -- a later notice looks like it changes something, and nobody
  has confirmed that. Acting on the old date is the risk.
- **quarantine** -- something arrived that could not be trusted enough to read.
  A guardian has to vouch for it or it never becomes anything.

"Coming up" means here what it means to the reminder sweep: the widest of
`reminder_lead_days`. The brief and the reminders should never disagree about
what is near.

Visibility is the same rule as every other read, inlined from
`artifacts.VISIBLE_TO_MEMBER`: a private notice reaches its sender's brief and
nobody else's. Children have no brief -- they do not sign in, and an
obligation's subject is who it is *about*, not who is told.
"""
from __future__ import annotations

import datetime as dt
import logging

from familyos import artifacts, reconcile, reminders
from familyos.db import pool
from familyos.identity import Principal
from familyos.intake import mailbox

logger = logging.getLogger(__name__)

REASONS = ("missed", "mailbox", "overdue", "today", "undecided", "soon", "amendment", "quarantine")
RANK = {reason: index for index, reason in enumerate(REASONS)}
DEFAULT_LIMIT = 3

# Past this, a date is history rather than today's business: a form due six
# weeks ago that nobody dismissed should not sit at the top of the brief for
# the rest of the year. It is still in the obligations list, which is where
# "what did we miss?" is answered.
STALE_DAYS = 30


def window() -> int:
    """How far ahead "coming up" reaches -- the same distance the reminder
    sweep uses, so the two never disagree about what is near."""
    return max(reminders.lead_days())


_OBLIGATIONS = f"""
SELECT o.id, o.artifact_id, o.claim_id, o.action, o.title, o.due_date, o.due_time,
       o.optional, o.status, o.subject_member_id, c.subject_name,
       EXISTS (SELECT 1 FROM amendments am
               WHERE am.amends_claim_id = o.claim_id AND am.status = 'proposed') AS contested
FROM obligations o
JOIN input_artifacts a ON a.id = o.artifact_id
LEFT JOIN claims c ON c.id = o.claim_id
WHERE {artifacts.VISIBLE_TO_MEMBER}
  AND o.status IN ('accepted', 'proposed')
  AND o.superseded_at IS NULL
  AND o.due_date IS NOT NULL
  AND o.due_date BETWEEN $3::date AND $4::date
ORDER BY o.due_date, o.created_at
"""

_WHY = {
    "missed": "nobody accepted or dismissed this, and the date has gone",
    "overdue": "the family accepted this, and the date has gone",
    "today": "today",
    "undecided": "nobody has accepted or dismissed this yet",
    "soon": "coming up",
}


def when_words(due: dt.date, today: dt.date) -> str:
    days = (due - today).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    if days < 0:
        return f"{-days} days ago"
    return f"in {days} days"


def reason_for(status: str, due: dt.date, today: dt.date) -> str:
    """Which bucket a dated obligation falls in. Whether anyone decided
    matters more than the date: a thing nobody answered is the thing that
    gets missed."""
    if due == today:
        return "today"
    if due < today:
        return "missed" if status == "proposed" else "overdue"
    return "undecided" if status == "proposed" else "soon"


def _item(reason: str, title: str, why: str, **rest) -> dict:
    return {"reason": reason, "rank": RANK[reason], "title": title, "why": why, **rest}


async def _obligations(p: Principal, today: dt.date) -> list[dict]:
    rows = await pool().fetch(_OBLIGATIONS, p.household_id, p.member_id,
                              today - dt.timedelta(days=STALE_DAYS),
                              today + dt.timedelta(days=window()))
    out = []
    for row in rows:
        reason = reason_for(row["status"], row["due_date"], today)
        out.append(_item(
            reason, row["title"], _WHY[reason],
            when=when_words(row["due_date"], today),
            due_date=row["due_date"], due_time=row["due_time"], action=row["action"],
            optional=row["optional"], status=row["status"], contested=row["contested"],
            subject_member_id=row["subject_member_id"], subject_name=row["subject_name"],
            obligation_id=row["id"], artifact_id=row["artifact_id"]))
    return out


async def _amendments(p: Principal) -> list[dict]:
    rows = await reconcile.list_for(p, "proposed")
    return [_item(
        "amendment", row["amends_title"],
        "a later notice looks like it changes this, and nobody has confirmed it",
        due_date=row["amends_date"], amendment_id=row["id"], artifact_id=row["artifact_id"],
        contested=True)
        for row in rows]


async def _mailboxes(p: Principal) -> list[dict]:
    """The member's own connected mailboxes that have stopped working. Only
    they can reconnect one, and only they can see it."""
    return [_item(
        "mailbox", account["email"],
        "FamilyOS can no longer read this mailbox, so notices in it are not arriving",
        mailbox_id=account["id"])
        for account in await mailbox.list_for(p) if account["needs_reconnect"]]


async def _quarantine(p: Principal) -> list[dict]:
    """Guardians only, the same as the review queue itself: an item nobody can
    vouch for would otherwise sit there unread."""
    if not p.is_guardian:
        return []
    rows = await artifacts.list_quarantine(p)
    return [_item(
        "quarantine",
        row["filename"] or (row["source"] or {}).get("subject") or row["channel"],
        "arrived, but could not be trusted enough to read",
        artifact_id=row["id"])
        for row in rows]


async def today(p: Principal, *, on: dt.date | None = None, limit: int = DEFAULT_LIMIT) -> dict:
    """The brief, worst first. `counts` is everything found, not everything
    shown, so "and 4 more" is honest."""
    on = on or dt.date.today()
    found = (await _obligations(p, on) + await _mailboxes(p) + await _amendments(p)
             + await _quarantine(p))
    found.sort(key=lambda i: (i["rank"], i.get("due_date") or dt.date.max, i["title"]))

    counts = {reason: 0 for reason in REASONS}
    for item in found:
        counts[item["reason"]] += 1
    shown = found if limit is None or limit < 0 else found[:limit]
    return {"date": on, "member_id": p.member_id, "display_name": p.display_name,
            "items": shown, "counts": counts, "more": len(found) - len(shown)}
