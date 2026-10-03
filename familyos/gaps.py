"""What FamilyOS did not see.

Everything else records what arrived. The alpha's real question is the other
one: of the notices a family actually got, how many reached the system at all?
That cannot be answered from the artifacts, because a notice that never
arrived leaves no row behind to count.

So a member says so -- "you missed the swimming letter, it was on the school
portal" -- and the column saying where it lived decides three things that are
otherwise guesses:

===================  =======================================================
school_portal        the browser rail is worth building
school_app           the mobile rail is, if Play Integrity allows it
whatsapp_group       forwarding needs to be easier, not a new rail
paper, word of mouth a camera in a native app
email                the query or the matching is wrong -- and that is cheap
===================  =======================================================

A gap is a report, never a notice. It produces no claims and no obligations,
and nothing downstream reads it. The moment anything did, it would be a second
source of truth about what a school said, with no provenance behind it -- a
sentence somebody typed from memory, standing where a quote from a circular
should be. It exists to be counted.

Reported misses are a lower bound: a family does not report every one. So the
number worth acting on is the breakdown, not the rate.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from familyos import audit
from familyos.db import pool
from familyos.identity import Invalid, NotFound, Principal

logger = logging.getLogger(__name__)

WHERE = ("email", "whatsapp_group", "whatsapp_direct", "school_portal", "school_app",
         "other_app", "sms", "paper", "word_of_mouth", "unknown")

# What each answer would mean doing. Carried in the summary so the finding and
# its consequence are never separated -- a table of channel counts invites
# everyone to read their own preferred conclusion into it.
MEANS = {
    "email": "the query or the sender matching is wrong -- the cheapest of these to fix",
    "whatsapp_group": "forwarding has to get easier; a new rail would not help",
    "whatsapp_direct": "the number is not matched to a member, or they forwarded to the wrong place",
    "school_portal": "a browser rail would earn its keep",
    "school_app": "a mobile rail would, if the app runs on a virtual device at all",
    "other_app": "mobile-only, and worth knowing which app before costing anything",
    "sms": "no channel reads these yet",
    "paper": "needs a camera, which needs a native app",
    "word_of_mouth": "nothing can read this; it is the floor on what any system can capture",
    "unknown": "ask again -- an unknown here is a question nobody followed up",
}


async def report(p: Principal, *, title: str, lived_where: str, also_emailed: bool | None = None,
                 had_date: bool = False, noticed_on: dt.date | None = None,
                 note: str | None = None) -> dict:
    """A member says a notice never reached them here."""
    title = (title or "").strip()
    if not title:
        raise Invalid("say what the notice was, so the report can be counted against something")
    if lived_where not in WHERE:
        raise Invalid(f"lived_where must be one of: {', '.join(WHERE)}")
    gap_id = uuid.uuid4()
    async with pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO intake_gaps (id, household_id, reported_by, title, lived_where, also_emailed, "
            "had_date, noticed_on, note) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING *",
            gap_id, p.household_id, p.member_id, title[:300], lived_where, also_emailed, had_date,
            noticed_on, (note or "").strip()[:2000] or None)
        # The title is the family's words about a notice, so it is not repeated
        # into the audit trail -- the same rule that keeps filenames out of it.
        await audit.record(p.household_id, "intake.gap_reported", actor_member_id=p.member_id,
                           target_type="intake_gap", target_id=gap_id,
                           detail={"lived_where": lived_where, "had_date": had_date,
                                   "also_emailed": also_emailed}, conn=conn)
    return dict(row)


async def list_for(p: Principal, limit: int = 200) -> list[dict]:
    """A guardian sees the household's reports; anyone else sees their own.
    The same shape as the audit trail, and for the same reason: working out
    what intake is missing is a guardian's job."""
    rows = await pool().fetch(
        "SELECT * FROM intake_gaps WHERE household_id = $1 AND ($2 OR reported_by = $3) "
        "ORDER BY created_at DESC LIMIT $4", p.household_id, p.is_guardian, p.member_id, limit)
    return [dict(r) for r in rows]


async def resolve(p: Principal, gap_id: uuid.UUID, artifact_id: uuid.UUID | None) -> dict:
    """Point a gap at the notice that eventually arrived, so "we fixed that
    one" is answerable. Passing nothing unsets it."""
    async with pool().acquire() as conn, conn.transaction():
        gap = await conn.fetchrow(
            "SELECT * FROM intake_gaps WHERE id = $1 AND household_id = $2 FOR UPDATE",
            gap_id, p.household_id)
        if gap is None:
            raise NotFound("gap report")
        if artifact_id is not None:
            # Only a notice this member can see, so resolving is not a way to
            # learn that a private artifact exists.
            seen = await conn.fetchval(
                "SELECT 1 FROM input_artifacts a WHERE a.id = $1 AND a.household_id = $2 "
                "AND a.status = 'accepted' AND (a.visibility = 'shared' OR a.submitted_by = $3)",
                artifact_id, p.household_id, p.member_id)
            if not seen:
                raise NotFound("artifact")
        row = await conn.fetchrow(
            "UPDATE intake_gaps SET arrived_as = $2 WHERE id = $1 RETURNING *", gap_id, artifact_id)
        await audit.record(p.household_id, "intake.gap_resolved", actor_member_id=p.member_id,
                           target_type="intake_gap", target_id=gap_id,
                           detail={"arrived_as": str(artifact_id) if artifact_id else None}, conn=conn)
    return dict(row)


async def summary(p: Principal, *, since: dt.date | None = None) -> dict:
    """Capture rate, and -- the part that decides anything -- where the misses
    lived.

    `captured` counts the notices that arrived; `missed` counts the ones
    somebody said should have. Both are per household and only what this
    member can see, so two members of one household can get different
    numbers; that is the visibility rule, not an error.

    The rate is an estimate and the docstring says so because the endpoint
    cannot: a family reports some of what it misses, never all, so `captured /
    (captured + missed)` is an upper bound on capture. The breakdown underneath
    is the reliable part.
    """
    since = since or (dt.date.today() - dt.timedelta(days=90))
    async with pool().acquire() as conn:
        captured = await conn.fetchval(
            "SELECT count(*) FROM input_artifacts a WHERE a.household_id = $1 AND a.status = 'accepted' "
            "AND a.parent_id IS NULL AND (a.visibility = 'shared' OR a.submitted_by = $2) "
            "AND a.received_at >= $3::date", p.household_id, p.member_id, since)
        by_channel = {r["channel"]: r["n"] for r in await conn.fetch(
            "SELECT a.channel, count(*) AS n FROM input_artifacts a WHERE a.household_id = $1 "
            "AND a.status = 'accepted' AND a.parent_id IS NULL "
            "AND (a.visibility = 'shared' OR a.submitted_by = $2) AND a.received_at >= $3::date "
            "GROUP BY a.channel", p.household_id, p.member_id, since)}
        gaps = await conn.fetch(
            "SELECT lived_where, count(*) AS n, count(*) FILTER (WHERE had_date) AS dated, "
            "count(*) FILTER (WHERE also_emailed) AS also_emailed, "
            "count(*) FILTER (WHERE arrived_as IS NOT NULL) AS arrived_later "
            "FROM intake_gaps WHERE household_id = $1 AND ($2 OR reported_by = $3) "
            "AND created_at >= $4::date GROUP BY lived_where ORDER BY count(*) DESC",
            p.household_id, p.is_guardian, p.member_id, since)

    missed = sum(r["n"] for r in gaps)
    total = captured + missed
    return {
        "since": since,
        "captured": captured,
        "missed_reported": missed,
        # None rather than 1.0 when nothing has happened yet: a household with
        # no notices has not captured everything, it has captured nothing.
        "capture_rate": round(captured / total, 3) if total else None,
        "captured_by_channel": by_channel,
        "missed_by_where": [
            {"lived_where": r["lived_where"], "count": r["n"], "with_a_date": r["dated"],
             "also_emailed": r["also_emailed"], "arrived_later": r["arrived_later"],
             "means": MEANS[r["lived_where"]]}
            for r in gaps],
        "caveat": ("Reported misses are a lower bound, so the rate is an upper bound on capture. "
                   "The breakdown is what decides anything."),
    }


__all__ = ["MEANS", "WHERE", "list_for", "report", "resolve", "summary"]
