"""Verifiable parental consent for children's data (DPDP Act 2023, s.9).

Nothing may be recorded about a child member until a guardian of the same
household has given consent for that purpose. Withdrawing consent starts
an erasure of everything that names the child (erasure.py); the consent
record itself is kept, as the evidence that consent existed and ended.
"""
from __future__ import annotations

import uuid

import asyncpg

from familyos import audit
from familyos.db import pool
from familyos.identity import Invalid, NotAllowed, NotFound, Principal
from familyos.models import ConsentIn

HOUSEHOLD_RECORDS = "household_records"


class ConsentRequired(Exception):
    def __init__(self, member_ids: list[uuid.UUID]):
        super().__init__("consent required for " + ", ".join(map(str, member_ids)))
        self.member_ids = member_ids


async def grant(actor: Principal, data: ConsentIn) -> dict:
    if not actor.is_guardian:
        raise NotAllowed("only a guardian can give consent for a child")
    async with pool().acquire() as conn, conn.transaction():
        subject = await conn.fetchrow("SELECT role FROM members WHERE id = $1 AND household_id = $2",
                                      data.subject_member_id, actor.household_id)
        if subject is None:
            raise NotFound("member")
        if subject["role"] != "child":
            raise Invalid("consent records are for child members")
        try:
            row = await conn.fetchrow(
                "INSERT INTO consent_records (id, household_id, subject_member_id, guardian_member_id, purpose, "
                "notice_version, verification_method) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
                uuid.uuid4(), actor.household_id, data.subject_member_id, actor.member_id, data.purpose,
                data.notice_version, data.verification_method)
        except asyncpg.UniqueViolationError as exc:
            raise Invalid("consent for this purpose is already active") from exc
        await audit.record(actor.household_id, "consent.granted", actor_member_id=actor.member_id, target_type="consent",
                           target_id=row["id"], detail={"subject_member_id": str(data.subject_member_id),
                                                        "purpose": data.purpose, "notice_version": data.notice_version},
                           conn=conn)
    return dict(row)


async def withdraw(actor: Principal, consent_id: uuid.UUID, conn: asyncpg.Connection) -> dict:
    """Marks the consent withdrawn, in the caller's transaction; the caller
    starts the erasure in the same one (erasure.withdraw_consent)."""
    if not actor.is_guardian:
        raise NotAllowed("only a guardian can withdraw consent")
    row = await conn.fetchrow(
        "UPDATE consent_records SET withdrawn_at = NOW(), withdrawn_by = $3 "
        "WHERE id = $1 AND household_id = $2 AND withdrawn_at IS NULL RETURNING *",
        consent_id, actor.household_id, actor.member_id)
    if row is None:
        raise NotFound("active consent")
    await audit.record(actor.household_id, "consent.withdrawn", actor_member_id=actor.member_id,
                       target_type="consent", target_id=consent_id,
                       detail={"subject_member_id": str(row["subject_member_id"])}, conn=conn)
    return dict(row)


async def list_for(household_id: uuid.UUID) -> list[dict]:
    rows = await pool().fetch("SELECT * FROM consent_records WHERE household_id = $1 ORDER BY granted_at DESC", household_id)
    return [dict(r) for r in rows]


async def require(conn: asyncpg.Connection, household_id: uuid.UUID, member_ids: list[uuid.UUID],
                  purpose: str = HOUSEHOLD_RECORDS) -> None:
    """Every named member belongs to the household, and every child among
    them has active consent for `purpose`."""
    if not member_ids:
        return
    members = await conn.fetch("SELECT id, role FROM members WHERE household_id = $1 AND id = ANY($2::uuid[])",
                               household_id, list(member_ids))
    if len(members) != len(set(member_ids)):
        raise NotFound("member")
    children = [m["id"] for m in members if m["role"] == "child"]
    if not children:
        return
    # FOR SHARE: a withdrawal committing now waits for this intake, and its
    # erasure then sees what this intake recorded.
    consented = {r["subject_member_id"] for r in await conn.fetch(
        "SELECT subject_member_id FROM consent_records WHERE subject_member_id = ANY($1::uuid[]) "
        "AND purpose = $2 AND withdrawn_at IS NULL FOR SHARE", children, purpose)}
    missing = [c for c in children if c not in consented]
    if missing:
        raise ConsentRequired(missing)
