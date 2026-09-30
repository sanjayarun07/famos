"""Erasure: the deletion hooks behind consent withdrawal and "delete my
family's data".

Both kinds run as durable jobs, so an erasure that is interrupted resumes
where it stopped instead of leaving data half-deleted. Each step is safe to
repeat. What an erasure did is kept in `erasure_log`, which holds no
personal data.

- Subject erasure: everything that names one member (usually a child) as
  its subject. Started when a guardian withdraws consent, or erases a child
  member outright.
- Household erasure: everything. The household key is destroyed first, so
  the originals are unreadable even before their objects are deleted, and
  including any copy in a backup. Then objects, rows and the household's
  audit trail go.
"""
from __future__ import annotations

import uuid

from familyos import artifacts, audit, consent, jobs
from familyos.db import pool
from familyos.identity import Invalid, NotAllowed, NotFound, Principal

SUBJECT = "erase_subject"
HOUSEHOLD = "erase_household"


async def request_subject_erasure(actor: Principal, member_id: uuid.UUID, *, reason: str,
                                  delete_member: bool, conn=None) -> dict:
    if not actor.is_guardian:
        raise NotAllowed("only a guardian can erase a member's data")
    if conn is None:
        async with pool().acquire() as c, c.transaction():
            return await request_subject_erasure(actor, member_id, reason=reason, delete_member=delete_member, conn=c)
    member = await conn.fetchrow("SELECT role FROM members WHERE id = $1 AND household_id = $2", member_id, actor.household_id)
    if member is None:
        raise NotFound("member")
    if member["role"] != "child":
        raise NotAllowed("v1 erases child members; adults close their own account")
    erasure_id = uuid.uuid4()
    job = await jobs.create(SUBJECT, {"erasure_id": str(erasure_id), "household_id": str(actor.household_id),
                                      "member_id": str(member_id), "delete_member": delete_member},
                            household_id=actor.household_id, member_id=actor.member_id, conn=conn)
    row = await conn.fetchrow(
        "INSERT INTO erasure_log (id, scope, household_id, subject_member_id, job_id, reason) "
        "VALUES ($1, 'subject', $2, $3, $4, $5) RETURNING *", erasure_id, actor.household_id, member_id,
        uuid.UUID(job["id"]), reason)
    await audit.record(actor.household_id, "erasure.requested", actor_member_id=actor.member_id, target_type="member",
                       target_id=member_id, detail={"erasure_id": str(erasure_id), "scope": "subject", "reason": reason,
                                                    "delete_member": delete_member}, conn=conn)
    return {**dict(row), "status": job["status"]}


async def withdraw_consent(actor: Principal, consent_id: uuid.UUID) -> dict:
    """Withdraw a child's consent and start erasing what names them, as one
    change: there is never a withdrawn consent without its erasure."""
    async with pool().acquire() as conn, conn.transaction():
        row = await consent.withdraw(actor, consent_id, conn)
        return await request_subject_erasure(actor, row["subject_member_id"], reason="consent_withdrawn",
                                             delete_member=False, conn=conn)


async def request_household_erasure(actor: Principal, confirm_name: str) -> dict:
    if not actor.is_guardian:
        raise NotAllowed("only a guardian can erase the household")
    async with pool().acquire() as conn, conn.transaction():
        household = await conn.fetchrow("SELECT name, status FROM households WHERE id = $1 FOR UPDATE", actor.household_id)
        if household is None or household["status"] != "active":
            raise NotFound("household")
        if confirm_name.strip() != household["name"]:
            raise Invalid("type the household's name exactly to confirm")
        # Stop intake and sign-in now, before the job gets to it.
        await conn.execute("UPDATE households SET status = 'erasing' WHERE id = $1", actor.household_id)
        erasure_id = uuid.uuid4()
        # A system job: it must outlive the household it deletes.
        job = await jobs.create(HOUSEHOLD, {"erasure_id": str(erasure_id), "household_id": str(actor.household_id)}, conn=conn)
        row = await conn.fetchrow(
            "INSERT INTO erasure_log (id, scope, household_id, job_id, reason) VALUES ($1, 'household', $2, $3, 'household_request') "
            "RETURNING *", erasure_id, actor.household_id, uuid.UUID(job["id"]))
    return {**dict(row), "status": job["status"]}


async def get(household_id: uuid.UUID, erasure_id: uuid.UUID) -> dict:
    row = await pool().fetchrow(
        "SELECT e.*, j.status FROM erasure_log e LEFT JOIN jobs j ON j.id = e.job_id WHERE e.id = $1 AND e.household_id = $2",
        erasure_id, household_id)
    if row is None:
        raise NotFound("erasure")
    return dict(row)


# ----------------------------------------------------------------------------
# handlers
# ----------------------------------------------------------------------------

async def _erase_subject(job: dict, ctx: jobs.JobContext) -> dict:
    spec = job["spec"]
    household_id, member_id = uuid.UUID(spec["household_id"]), uuid.UUID(spec["member_id"])
    await ctx.plan(["Delete records that name the member", "Delete their originals from storage", "Record the erasure"])
    counts = dict((ctx.job.get("state") or {}).get("counts") or {})

    if not ctx.done("0"):
        await ctx.step("0")
        async with pool().acquire() as conn, conn.transaction():
            # Read the name before the member row can go: claims may name the
            # child on artifacts that were never linked to them as a subject,
            # and those artifacts are not the child's to delete.
            named = await conn.fetchval("SELECT display_name FROM members WHERE id = $1 AND household_id = $2",
                                        member_id, household_id)
            blob_ids = [r["blob_id"] for r in await conn.fetch(
                "SELECT blob_id FROM input_artifacts WHERE household_id = $1 AND (id IN "
                "(SELECT artifact_id FROM artifact_subjects WHERE member_id = $2) OR parent_id IN "
                "(SELECT artifact_id FROM artifact_subjects WHERE member_id = $2))", household_id, member_id)]
            deleted = await conn.fetch(
                "DELETE FROM input_artifacts WHERE household_id = $1 AND id IN "
                "(SELECT artifact_id FROM artifact_subjects WHERE member_id = $2) RETURNING id", household_id, member_id)
            keys = await artifacts.drop_orphan_blobs(conn, household_id, blob_ids)
            # Extraction jobs for those artifacts may still hold a cached model answer.
            await conn.execute("DELETE FROM jobs WHERE household_id = $1 AND kind = 'extract_artifact' "
                               "AND spec->>'artifact_id' = ANY($2::text[])", household_id, [str(r["id"]) for r in deleted])
            # What survives the delete above is a notice that merely mentions
            # the child. The fact stays; their name does not.
            cleared = [r["id"] for r in await conn.fetch(
                "SELECT id, subject_name FROM claims WHERE household_id = $1 AND subject_name IS NOT NULL", household_id)
                if consent.names_match(r["subject_name"], named)] if named else []
            if cleared:
                await conn.execute("UPDATE claims SET subject_name = NULL WHERE id = ANY($1::uuid[])", cleared)
            member_deleted = False
            if spec.get("delete_member"):
                member_deleted = (await conn.execute("DELETE FROM members WHERE id = $1 AND household_id = $2",
                                                     member_id, household_id)) != "DELETE 0"
        counts.update({"artifacts": len(deleted), "blobs": len(keys), "member_deleted": member_deleted,
                       "names_cleared": len(cleared)})
        await ctx.checkpoint(state={"counts": counts, "pending_keys": keys})
        await ctx.finish_step("0")

    if not ctx.done("1"):
        await ctx.step("1")
        failed = await artifacts.delete_objects((ctx.job.get("state") or {}).get("pending_keys") or [])
        if failed:
            raise RuntimeError(f"{len(failed)} objects could not be deleted; will retry")
        await ctx.checkpoint(state={"counts": counts, "pending_keys": []})
        await ctx.finish_step("1")

    if not ctx.done("2"):
        await ctx.step("2")
        async with pool().acquire() as conn, conn.transaction():
            await conn.execute("UPDATE erasure_log SET completed_at = COALESCE(completed_at, NOW()), counts = $2 WHERE id = $1",
                               uuid.UUID(spec["erasure_id"]), counts)
            await audit.record(household_id, "erasure.completed", actor_kind="system", target_type="member",
                               target_id=member_id, detail={"erasure_id": spec["erasure_id"], **counts}, conn=conn)
        await ctx.finish_step("2")
    return {"counts": counts}


async def _erase_household(job: dict, ctx: jobs.JobContext) -> dict:
    spec = job["spec"]
    household_id = uuid.UUID(spec["household_id"])
    await ctx.plan(["Destroy the household key", "Delete originals from storage", "Delete records and audit trail",
                    "Record the erasure"])
    counts = dict((ctx.job.get("state") or {}).get("counts") or {})

    if not ctx.done("0"):
        await ctx.step("0")
        await pool().execute("UPDATE households SET status = 'erasing', wrapped_key = NULL WHERE id = $1", household_id)
        await ctx.finish_step("0")

    if not ctx.done("1"):
        await ctx.step("1")
        keys = [r["storage_key"] for r in await pool().fetch("SELECT storage_key FROM blobs WHERE household_id = $1", household_id)]
        failed = await artifacts.delete_objects(keys)
        if failed:
            raise RuntimeError(f"{len(failed)} objects could not be deleted; will retry")
        counts["blobs"] = len(keys)
        await ctx.checkpoint(state={"counts": counts})
        await ctx.finish_step("1")

    if not ctx.done("2"):
        await ctx.step("2")
        async with pool().acquire() as conn, conn.transaction():
            counts["artifacts"] = await conn.fetchval("SELECT count(*) FROM input_artifacts WHERE household_id = $1", household_id)
            counts["members"] = await conn.fetchval("SELECT count(*) FROM members WHERE household_id = $1", household_id)
            await conn.execute("SET LOCAL familyos.audit_purge = 'on'")
            await conn.execute("DELETE FROM audit_events WHERE household_id = $1", household_id)
            await conn.execute("DELETE FROM households WHERE id = $1", household_id)
        await ctx.checkpoint(state={"counts": counts})
        await ctx.finish_step("2")

    if not ctx.done("3"):
        await ctx.step("3")
        await pool().execute("UPDATE erasure_log SET completed_at = COALESCE(completed_at, NOW()), counts = $2 WHERE id = $1",
                             uuid.UUID(spec["erasure_id"]), counts)
        await ctx.finish_step("3")
    return {"counts": counts}


def register() -> None:
    jobs.register(SUBJECT, _erase_subject)
    jobs.register(HOUSEHOLD, _erase_household)

