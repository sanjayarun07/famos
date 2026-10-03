"""Extraction as a durable job after intake, and the records it leaves.

Every accepted artifact gets an `extract_artifact` job in the same
transaction that accepted it (an upload, an authenticated email and its
attachments, or a quarantined item a guardian vouched for). The job reads
the original, extracts grounded claims, and stores them with the
obligations proposed from them.

The model's answer is cached in the job while it runs, so a job that is
interrupted after the call resumes without paying for it again. Once the
claims are stored the cache is cleared: notice text lives in the claims
tables, which cascade with the artifact on erasure, and nowhere else.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import uuid

import asyncpg

from familyos import artifacts, audit, consent, jobs, reconcile
from familyos.db import pool
from familyos.extraction import pipeline
from familyos.extraction.claims import Extraction
from familyos.extraction.llm import ModelExtractor
from familyos.extraction.obligations import propose
from familyos.extraction.parse import parse
from familyos.identity import Invalid, NotFound, Principal
from familyos.settings import settings

KIND = "extract_artifact"
HANDLER_VERSION = "1"

# What a claim is worth when it was relayed rather than issued. The same shape
# as pipeline.UNGROUNDED_PENALTY: kept and shown, trusted less.
HEARSAY_PENALTY = 0.8

# What a claim is worth when its page was read by OCR rather than lifted from a
# text layer. Characters get confused in ways that matter here: a Devanagari
# notice read in testing turned "14 नवंबर" into "44 नवंबर", which on a deadline
# is not a small error.
OCR_PENALTY = 0.9


async def enqueue(conn: asyncpg.Connection, household_id: uuid.UUID, artifact_ids: list[uuid.UUID], *,
                  member_id: uuid.UUID | None = None) -> list[dict]:
    """Queue extraction for accepted artifacts, inside the caller's transaction."""
    if not settings.extraction_enabled:
        return []
    return [await jobs.create(KIND, {"household_id": str(household_id), "artifact_id": str(a)},
                              household_id=household_id, member_id=member_id, conn=conn) for a in artifact_ids]


def reference_date(artifact: dict) -> dt.date:
    """The day the notice counts as read on: the email's Date header when
    there is one, otherwise the day it arrived."""
    sent = (artifact.get("source") or {}).get("date")
    if sent:
        try:
            return email.utils.parsedate_to_datetime(sent).date()
        except (TypeError, ValueError):
            pass
    return (artifact.get("received_at") or dt.datetime.now(dt.UTC)).date()


# ----------------------------------------------------------------------------
# the job
# ----------------------------------------------------------------------------

async def _handle(job: dict, ctx: jobs.JobContext) -> dict:
    spec = job["spec"]
    household_id, artifact_id = uuid.UUID(spec["household_id"]), uuid.UUID(spec["artifact_id"])
    await ctx.plan(["Read the notice", "Save claims and proposed tasks"])
    artifact = await _accepted_artifact(household_id, artifact_id)
    if artifact is None:
        await ctx.checkpoint(operations={})
        return {"skipped": "artifact no longer available"}
    if ctx.done("1"):
        return dict((ctx.job.get("state") or {}).get("result") or {})

    await ctx.step("0")
    data = await artifacts.read_bytes(household_id, artifact)
    doc = await asyncio.to_thread(parse, data, artifact["media_type"])
    ref = reference_date(artifact)
    extractor = pipeline.make_extractor()
    if isinstance(extractor, ModelExtractor) and doc.char_count:
        response = await ctx.call(
            "extract", lambda: extractor.call(doc, ref),
            args={"sha256": artifact["sha256"], "model": extractor.model, "effort": extractor.effort,
                  "prompt": extractor.prompt_version, "parser": doc.parser_version, "ref": ref.isoformat()})
        extraction = pipeline.from_model_response(doc, response, extractor, ref)
    else:
        extraction = await pipeline.extract(doc, ref, extractor)
    await ctx.finish_step("0")

    await ctx.step("1")
    result = await save(household_id, artifact_id, extraction, job_id=uuid.UUID(ctx.id))
    # The cached answer holds notice text; it is stored in the claims now.
    await ctx.checkpoint(operations={}, state={"result": result})
    await ctx.finish_step("1")
    return result


async def _accepted_artifact(household_id: uuid.UUID, artifact_id: uuid.UUID) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM input_artifacts WHERE id = $1 AND household_id = $2 AND status = 'accepted'",
                                artifact_id, household_id)
    return dict(row) if row else None


def _permitted_name(subject_name: str | None, allowed: list[tuple[uuid.UUID, str]]) -> str | None:
    if not subject_name:
        return None
    return subject_name if any(consent.names_match(subject_name, d) for _, d in allowed) else None


async def save(household_id: uuid.UUID, artifact_id: uuid.UUID, extraction: Extraction, *,
               job_id: uuid.UUID | None = None) -> dict:
    """Store an extraction, its claims and proposed obligations; supersede the
    previous extraction and replace its still-proposed obligations."""
    proposed = propose(extraction.claims, extraction.reference_date)
    async with pool().acquire() as conn, conn.transaction():
        artifact = await conn.fetchrow("SELECT id, parent_id, source FROM input_artifacts WHERE id = $1 "
                                       "AND household_id = $2 FOR KEY SHARE", artifact_id, household_id)
        if artifact is None:
            return {"skipped": "artifact deleted"}
        subjects = await conn.fetch("SELECT DISTINCT member_id FROM artifact_subjects WHERE artifact_id = $1 OR artifact_id = $2",
                                    artifact_id, artifact["parent_id"])
        subject = subjects[0]["member_id"] if len(subjects) == 1 else None
        # A claim may name a child only when that child has active consent,
        # the same rule artifact_subjects enforces. An extractor that reads a
        # name off the notice does not get to record it otherwise, and a name
        # matching nobody is dropped rather than kept on the chance it is safe.
        # A message a parent forwarded from their own group is second-hand.
        # It is usually the fastest way a family hears anything -- and it is
        # still somebody’s retelling, so it is not worth as much as the
        # school’s own circular saying the same thing.
        hearsay = bool((artifact["source"] or {}).get("forwarded"))
        from_ocr = extraction.ocr_pages > 0
        confidence_factor = (HEARSAY_PENALTY if hearsay else 1.0) * (OCR_PENALTY if from_ocr else 1.0)
        allowed = await consent.consented_children(conn, household_id)
        named = [c.subject_name for c in extraction.claims if c.subject_name]
        dropped = sum(1 for n in named if not any(consent.names_match(n, d) for _, d in allowed))
        await conn.execute("UPDATE extractions SET superseded_at = NOW() WHERE artifact_id = $1 AND superseded_at IS NULL",
                           artifact_id)
        decided = {(r["title"], r["due_date"]) for r in await conn.fetch(
            "SELECT title, due_date FROM obligations WHERE artifact_id = $1 AND status <> 'proposed'", artifact_id)}
        await conn.execute("DELETE FROM obligations WHERE artifact_id = $1 AND status = 'proposed'", artifact_id)
        extraction_id = uuid.uuid4()
        x = extraction.extractor
        await conn.execute(
            "INSERT INTO extractions (id, household_id, artifact_id, job_id, extractor, model, prompt_version, parser_version, "
            "reference_date, actionable, non_actionable_reason, page_count, ocr_pages, notes) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
            extraction_id, household_id, artifact_id, job_id, x.name, x.model, x.prompt_version, x.parser_version,
            extraction.reference_date, extraction.actionable, extraction.non_actionable_reason, extraction.page_count,
            extraction.ocr_pages, extraction.notes)
        claim_ids = [uuid.uuid4() for _ in extraction.claims]
        await conn.executemany(
            "INSERT INTO claims (id, extraction_id, household_id, artifact_id, ordinal, kind, title, date, end_date, date_text, "
            "time_text, place, amount, currency, amount_text, applies_to, subject_name, requires, optional, uncertain, amends, "
            "change, quote, page, span_start, span_end, boxes, match, confidence) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, "
            "$10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29)",
            [(claim_ids[i], extraction_id, household_id, artifact_id, i, c.kind, c.title, c.date, c.end_date, c.date_text,
              c.time, c.place, c.amount.value if c.amount else None, c.amount.currency if c.amount else None,
              c.amount.text if c.amount else None, c.applies_to, _permitted_name(c.subject_name, allowed),
              list(c.requires), c.optional, c.uncertain,
              c.amends, c.change, c.quote, c.page, (c.location or {}).get("start"), (c.location or {}).get("end"),
              (c.location or {}).get("boxes"), (c.location or {}).get("match"),
              round(c.confidence * confidence_factor, 3))
             for i, c in enumerate(extraction.claims)])
        rows = [(uuid.uuid4(), household_id, artifact_id, claim_ids[o.claim_index], subject, o.kind, o.action, o.title,
                 o.due_date, o.end_date, o.due_time, o.optional)
                for o in proposed if (o.title, o.due_date) not in decided]
        await conn.executemany(
            "INSERT INTO obligations (id, household_id, artifact_id, claim_id, subject_member_id, kind, action, title, due_date, "
            "end_date, due_time, optional) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)", rows)
        unsupported = await _supersede_unsupported(conn, household_id, artifact_id, proposed)
        # Now the claims exist, see whether any of them revises an earlier
        # notice, and whether this is simply the same notice arriving again.
        amendments = await reconcile.propose(conn, household_id, artifact_id)
        merged = await reconcile.merge_exact_duplicate(conn, household_id, artifact_id)
        counts = {"amendments_proposed": amendments, "extraction_id": str(extraction_id), "claims": len(claim_ids),
                  "grounded": sum(1 for c in extraction.claims if c.grounded), "obligations": len(rows),
                  "actionable": extraction.actionable, "extractor": x.name, "model": x.model,
                  "prompt_version": x.prompt_version, "names_dropped": dropped, "hearsay": hearsay,
                  "scripts": extraction.scripts, "from_ocr": from_ocr,
                  "accepted_superseded": unsupported,
                  "duplicate_of": (merged or {}).get("first_artifact_id"),
                  "obligations_superseded": (merged or {}).get("obligations_superseded", 0)}
        await audit.record(household_id, "extraction.completed", actor_kind="system", target_type="artifact",
                           target_id=artifact_id, detail=counts, conn=conn)
    return counts


# ----------------------------------------------------------------------------
# reading and deciding
# ----------------------------------------------------------------------------

async def _supersede_unsupported(conn: asyncpg.Connection, household_id: uuid.UUID, artifact_id: uuid.UUID,
                                 proposed: list) -> int:
    """A re-read of the same notice can disagree with a decision already made.

    Deleting the still-proposed obligations is safe -- nobody answered them.
    An *accepted* one is different: a person said yes to it, and that decision
    is theirs, not ours to drop. But leaving it untouched is not safe either.
    If the new reading moves the date, or no longer finds the claim at all, the
    accepted row goes on naming a date the notice no longer gives -- and goes
    on reminding about it, which is the one thing this is all for.

    So neither: an accepted obligation the new reading no longer supports is
    superseded, by this same notice. Superseded means reminders stop (the
    scheduler skips it and the sweep cancels what is pending), the row stays
    where the family can see what happened to it, and the corrected date
    arrives beside it as a fresh proposal to accept. What a person decided is
    recorded; what the notice says now is what the family is reminded of.
    """
    titles = [o.title for o in proposed]
    dates = [o.due_date for o in proposed]
    rows = await conn.fetch(
        "UPDATE obligations o SET superseded_at = NOW(), superseded_by_artifact_id = $2 "
        "WHERE o.household_id = $1 AND o.artifact_id = $2 AND o.status = 'accepted' AND o.superseded_at IS NULL "
        # NOT DISTINCT FROM so an undated obligation matches an undated
        # proposal rather than matching nothing.
        "AND NOT EXISTS (SELECT 1 FROM unnest($3::text[], $4::date[]) AS n(title, due_date) "
        "                WHERE n.title = o.title AND n.due_date IS NOT DISTINCT FROM o.due_date) "
        "RETURNING o.id", household_id, artifact_id, titles, dates)
    return len(rows)


async def get_for_artifact(p: Principal, artifact_id: uuid.UUID) -> dict:
    """The current extraction of an artifact the member can see, with its
    claims; or the state of the job still working on it."""
    await artifacts.get_visible(p, artifact_id)
    row = await pool().fetchrow("SELECT * FROM extractions WHERE artifact_id = $1 AND superseded_at IS NULL", artifact_id)
    job = await pool().fetchrow("SELECT id, status, error FROM jobs WHERE kind = $1 AND household_id = $2 "
                                "AND spec->>'artifact_id' = $3 ORDER BY created_at DESC LIMIT 1",
                                KIND, p.household_id, str(artifact_id))
    job_state = {"job_id": job["id"], "job_status": job["status"], "job_error": job["error"]} if job else \
        {"job_id": None, "job_status": None, "job_error": None}
    if row is None:
        return {"artifact_id": artifact_id, "extraction": None, **job_state}
    claims = await pool().fetch("SELECT * FROM claims WHERE extraction_id = $1 ORDER BY ordinal", row["id"])
    return {"artifact_id": artifact_id, "extraction": {**dict(row), "claims": [dict(c) for c in claims]}, **job_state}


async def rerun(p: Principal, artifact_id: uuid.UUID) -> dict:
    await artifacts.get_visible(p, artifact_id)
    async with pool().acquire() as conn, conn.transaction():
        created = await enqueue(conn, p.household_id, [artifact_id], member_id=p.member_id)
    if not created:
        raise Invalid("extraction is turned off")
    return jobs.public(created[0])


_VISIBLE_OBLIGATIONS = ("SELECT o.* FROM obligations o JOIN input_artifacts a ON a.id = o.artifact_id "
                        f"WHERE {artifacts.VISIBLE_TO_MEMBER}")


async def list_obligations(p: Principal, status: str | None = "proposed", limit: int = 100) -> list[dict]:
    rows = await pool().fetch(
        _VISIBLE_OBLIGATIONS + " AND ($3::text IS NULL OR o.status = $3) ORDER BY o.due_date NULLS LAST, o.created_at LIMIT $4",
        p.household_id, p.member_id, status, limit)
    return [dict(r) for r in rows]


async def decide(p: Principal, obligation_id: uuid.UUID, status: str) -> dict:
    if status not in ("accepted", "dismissed"):
        raise Invalid("status must be accepted or dismissed")
    async with pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(_VISIBLE_OBLIGATIONS + " AND o.id = $3 FOR UPDATE OF o", p.household_id, p.member_id,
                                  obligation_id)
        if row is None:
            raise NotFound("obligation")
        row = await conn.fetchrow("UPDATE obligations SET status = $2, decided_by = $3, decided_at = NOW() WHERE id = $1 "
                                  "RETURNING *", obligation_id, status, p.member_id)
        await audit.record(p.household_id, f"obligation.{status}", actor_member_id=p.member_id, target_type="obligation",
                           target_id=obligation_id, detail={"artifact_id": str(row["artifact_id"])}, conn=conn)
    return dict(row)


def register() -> None:
    jobs.register(KIND, _handle, version=HANDLER_VERSION,
                  settings_keys=("extractor", "extraction_model", "extraction_effort"))
