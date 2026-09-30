"""Reconciliation: joining a revised notice to the one it revises.

A school sends "Annual day, revised timings" and the earlier notice is still
sitting there with its own date. Extraction already reads an `amendment` claim
out of the new one, saying in the notice's own words what it changes
(`amends`) and what is now true (`change`). What was missing was the link from
that claim to the artifact it is talking about, so both notices reminded and
nothing said they were the same thing.

**A link is proposed, never applied on its own.** Matching two notices by their
words is a guess, and the cost of guessing wrong is asymmetric: a wrong link
stops the family being reminded about a deadline that still stands, which is
worse than being reminded twice. So a member confirms it. Until they do, both
notices still remind, and the reminder says a later notice may have changed
this.

Matching uses no model. An amendment claim's words are compared with the
titles of earlier claims in the same household, scored on shared words, and
nudged by whether the two are scoped to the same group and whether the earlier
claim's date is still ahead. It is deliberately conservative: a near miss
proposes nothing rather than guessing.
"""
from __future__ import annotations

import logging
import re
import uuid

import asyncpg

from familyos import audit
from familyos.db import pool
from familyos.identity import NotAllowed, NotFound, Principal
from familyos.settings import settings

logger = logging.getLogger(__name__)

# Words that say nothing about which notice is meant.
STOP = frozenset("""
a an the and or of for to on at in by with from is are was were be been being this that these those
it its as if then than so such not no nor but into onto upon about over under
will shall may might can could would should must
notice circular letter dear parents parent school students student kindly please
revised revision revise change changed changes update updated amendment amend amended
new now instead rescheduled postponed
""".split())

MIN_SCORE = 0.42
MIN_SHARED = 2


def words(text: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").casefold()) if w not in STOP and len(w) > 2}


def score(amendment: dict, candidate: dict) -> tuple[float, str]:
    """How much an amendment claim looks like it is talking about an earlier
    claim. Returns (score, what matched)."""
    said = words(amendment.get("amends")) | words(amendment.get("title"))
    theirs = words(candidate.get("title"))
    if not said or not theirs:
        return 0.0, "nothing to compare"
    shared = said & theirs
    if len(shared) < MIN_SHARED:
        return 0.0, "too little in common"
    base = len(shared) / len(said | theirs)

    reasons = [f"{len(shared)} shared word" + ("" if len(shared) == 1 else "s")]
    bonus = 0.0
    a_scope, c_scope = words(amendment.get("applies_to")), words(candidate.get("applies_to"))
    if a_scope and c_scope and (a_scope & c_scope):
        bonus += 0.15
        reasons.append("same group")
    if candidate.get("kind") in ("event", "deadline") and candidate.get("date"):
        bonus += 0.05
        reasons.append("dated")
    return min(1.0, base + bonus), ", ".join(reasons)


_CANDIDATES = """
SELECT c.id, c.artifact_id, c.title, c.applies_to, c.kind, c.date
FROM claims c
JOIN extractions x ON x.id = c.extraction_id AND x.superseded_at IS NULL
JOIN input_artifacts a ON a.id = c.artifact_id
WHERE c.household_id = $1
  AND c.artifact_id <> $2
  AND a.status = 'accepted'
  AND a.received_at <= $3
  AND c.kind IN ('event', 'deadline', 'payment', 'form')
"""


async def propose(conn: asyncpg.Connection, household_id: uuid.UUID, artifact_id: uuid.UUID) -> int:
    """Look for what this artifact's amendment claims are talking about, and
    record the links as proposals. Safe to repeat."""
    if not settings.reconciliation_enabled:
        return 0
    amendments = await conn.fetch(
        "SELECT c.id, c.title, c.amends, c.applies_to FROM claims c "
        "JOIN extractions x ON x.id = c.extraction_id AND x.superseded_at IS NULL "
        "WHERE c.household_id = $1 AND c.artifact_id = $2 AND c.kind = 'amendment'",
        household_id, artifact_id)
    if not amendments:
        return 0
    received = await conn.fetchval("SELECT received_at FROM input_artifacts WHERE id = $1", artifact_id)
    candidates = [dict(r) for r in await conn.fetch(_CANDIDATES, household_id, artifact_id, received)]
    if not candidates:
        return 0

    made = 0
    for raw in amendments:
        amendment = dict(raw)
        best, best_score, best_why = None, 0.0, ""
        for candidate in candidates:
            value, why = score(amendment, candidate)
            if value > best_score:
                best, best_score, best_why = candidate, value, why
        if best is None or best_score < MIN_SCORE:
            continue
        row = await conn.fetchrow(
            "INSERT INTO amendments (id, household_id, artifact_id, claim_id, amends_artifact_id, amends_claim_id, "
            "score, matched_on) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            "ON CONFLICT (claim_id, amends_claim_id) DO NOTHING RETURNING id",
            uuid.uuid4(), household_id, artifact_id, amendment["id"], best["artifact_id"], best["id"],
            round(best_score, 3), best_why)
        if row is None:
            continue
        made += 1
        await audit.record(household_id, "amendment.proposed", actor_kind="system", target_type="artifact",
                           target_id=artifact_id, detail={"amendment_id": str(row["id"]),
                                                          "amends_artifact_id": str(best["artifact_id"]),
                                                          "score": round(best_score, 3)}, conn=conn)
    return made


# ----------------------------------------------------------------------------
# reading and deciding
# ----------------------------------------------------------------------------

_VISIBLE = """
SELECT am.*, c.title AS claim_title, c.change, oc.title AS amends_title, oc.date AS amends_date,
       a.filename AS artifact_filename, a.received_at AS artifact_received_at,
       oa.filename AS amends_filename, oa.received_at AS amends_received_at
FROM amendments am
JOIN claims c ON c.id = am.claim_id
JOIN claims oc ON oc.id = am.amends_claim_id
JOIN input_artifacts a ON a.id = am.artifact_id
JOIN input_artifacts oa ON oa.id = am.amends_artifact_id
WHERE am.household_id = $1
  AND a.status = 'accepted' AND (a.visibility = 'shared' OR a.submitted_by = $2)
  AND oa.status = 'accepted' AND (oa.visibility = 'shared' OR oa.submitted_by = $2)
"""


async def list_for(p: Principal, status: str | None = "proposed", limit: int = 100) -> list[dict]:
    """Only links where the member can see BOTH notices: confirming one is
    saying these two things are the same, which needs sight of both."""
    rows = await pool().fetch(
        _VISIBLE + " AND ($3::text IS NULL OR am.status = $3) ORDER BY am.created_at DESC LIMIT $4",
        p.household_id, p.member_id, status, limit)
    return [dict(r) for r in rows]


async def decide(p: Principal, amendment_id: uuid.UUID, status: str) -> dict:
    """Confirm the link and the older notice's obligations are superseded and
    stop reminding; reject it and both stand."""
    if status not in ("confirmed", "rejected"):
        raise NotAllowed("an amendment is confirmed or rejected")
    async with pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(_VISIBLE + " AND am.id = $3 FOR UPDATE OF am", p.household_id, p.member_id,
                                  amendment_id)
        if row is None:
            raise NotFound("amendment")
        if row["status"] != "proposed":
            raise NotAllowed("this was already " + row["status"])
        await conn.execute(
            "UPDATE amendments SET status = $2, decided_by = $3, decided_at = NOW() WHERE id = $1",
            amendment_id, status, p.member_id)

        superseded = []
        if status == "confirmed":
            superseded = await conn.fetch(
                "UPDATE obligations SET superseded_at = NOW(), superseded_by_artifact_id = $1 "
                "WHERE claim_id = $2 AND superseded_at IS NULL RETURNING id",
                row["artifact_id"], row["amends_claim_id"])
            if superseded:
                await conn.execute(
                    "UPDATE reminders SET status = 'cancelled' WHERE status = 'pending' AND obligation_id = ANY($1::uuid[])",
                    [r["id"] for r in superseded])
        await audit.record(p.household_id, "amendment." + status, actor_member_id=p.member_id,
                           target_type="artifact", target_id=row["artifact_id"],
                           detail={"amendment_id": str(amendment_id),
                                   "amends_artifact_id": str(row["amends_artifact_id"]),
                                   "obligations_superseded": len(superseded)}, conn=conn)
    return {**dict(row), "status": status, "obligations_superseded": len(superseded)}
