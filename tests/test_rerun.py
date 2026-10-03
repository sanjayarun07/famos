"""Re-reading a notice the family has already answered.

Extraction is not final. A better model, a fixed prompt or a second pass can
read the same circular differently -- and by then somebody may have accepted
what the first pass said. Their decision is theirs; the date, though, has to
be the one the notice gives.
"""
import datetime as dt
import uuid

from familyos.extraction import service
from familyos.extraction.claims import Claim, Extraction, ExtractorInfo
from tests.conftest import pdf

GROUNDED = {"page": 1, "start": 0, "end": 16, "boxes": [[60.0, 78.0, 300.0, 93.0]], "match": "exact"}
TENTH = dt.date.today() + dt.timedelta(days=10)
SEVENTEENTH = dt.date.today() + dt.timedelta(days=17)


def _read(*claims: tuple[str, dt.date | None]):
    return Extraction(
        extractor=ExtractorInfo(name="openai", model="openai/gpt-5.6-sol",
                                prompt_version="extract-v2", parser_version="p1"),
        reference_date=dt.date.today(), actionable=True, page_count=1,
        claims=[Claim(kind="deadline", title=title, date=date, quote="Return the slip",
                      location=GROUNDED, confidence=0.9) for title, date in claims])


async def _artifact(family):
    return (await family.upload("amma", pdf("r"), visibility="shared")).json()["artifact"]["id"]


async def _accept(family, obligation_id):
    r = await family.client.post(f"/v1/obligations/{obligation_id}/decision", headers=family.h("amma"),
                                 json={"status": "accepted"})
    assert r.status_code == 200, r.text


async def _obligations(database, artifact_id):
    rows = await database.fetch(
        "SELECT title, due_date, status, superseded_at, superseded_by_artifact_id FROM obligations "
        "WHERE artifact_id = $1 ORDER BY status, due_date", uuid.UUID(artifact_id))
    return [dict(r) for r in rows]


async def test_a_moved_date_supersedes_the_accepted_obligation(family, database):
    """The second reading moves the deadline. The accepted row said the 10th
    and would have gone on reminding about the 10th; it is superseded, and the
    17th arrives beside it to be accepted."""
    household_id = uuid.UUID(family.household["id"])
    artifact_id = await _artifact(family)

    await service.save(household_id, uuid.UUID(artifact_id), _read(("Return the slip", TENTH)))
    first = await database.fetchrow("SELECT id FROM obligations WHERE artifact_id = $1", uuid.UUID(artifact_id))
    await _accept(family, first["id"])

    await service.save(household_id, uuid.UUID(artifact_id), _read(("Return the slip", SEVENTEENTH)))
    rows = await _obligations(database, artifact_id)
    accepted = [r for r in rows if r["status"] == "accepted"]
    proposed = [r for r in rows if r["status"] == "proposed"]

    assert [r["due_date"] for r in accepted] == [TENTH]
    assert accepted[0]["superseded_at"] is not None
    # Superseded by this same notice: it is the notice that changed its mind.
    assert accepted[0]["superseded_by_artifact_id"] == uuid.UUID(artifact_id)
    assert [r["due_date"] for r in proposed] == [SEVENTEENTH]


async def test_a_claim_the_second_reading_drops_supersedes_too(family, database):
    """The second reading no longer finds the deadline at all -- it was never
    one. Nothing replaces it, and the accepted row stops being a live date."""
    household_id = uuid.UUID(family.household["id"])
    artifact_id = await _artifact(family)

    await service.save(household_id, uuid.UUID(artifact_id),
                       _read(("Return the slip", TENTH), ("Pay the fee", TENTH)))
    slip = await database.fetchrow(
        "SELECT id FROM obligations WHERE artifact_id = $1 AND title = 'Return the slip'", uuid.UUID(artifact_id))
    await _accept(family, slip["id"])

    counts = await service.save(household_id, uuid.UUID(artifact_id), _read(("Pay the fee", TENTH)))
    assert counts["accepted_superseded"] == 1
    accepted = [r for r in await _obligations(database, artifact_id) if r["status"] == "accepted"]
    assert len(accepted) == 1 and accepted[0]["superseded_at"] is not None


async def test_a_reading_that_agrees_leaves_the_decision_alone(family, database):
    """The common case: the second pass says the same thing. The accepted row
    is untouched -- not superseded, and not asked about again."""
    household_id = uuid.UUID(family.household["id"])
    artifact_id = await _artifact(family)

    await service.save(household_id, uuid.UUID(artifact_id), _read(("Return the slip", TENTH)))
    first = await database.fetchrow("SELECT id FROM obligations WHERE artifact_id = $1", uuid.UUID(artifact_id))
    await _accept(family, first["id"])

    counts = await service.save(household_id, uuid.UUID(artifact_id), _read(("Return the slip", TENTH)))
    assert counts["accepted_superseded"] == 0
    rows = await _obligations(database, artifact_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "accepted" and rows[0]["superseded_at"] is None


async def test_a_superseded_obligation_stops_being_reminded_about(family, database, monkeypatch):
    """What the whole thing is for. A date nobody will be reminded of is the
    only way superseding is better than leaving it."""
    from familyos import reminders
    from familyos.db import pool
    from familyos.settings import settings
    monkeypatch.setattr(settings, "reminder_lead_days", "30")

    household_id = uuid.UUID(family.household["id"])
    artifact_id = await _artifact(family)
    await service.save(household_id, uuid.UUID(artifact_id), _read(("Return the slip", TENTH)))
    first = await database.fetchrow("SELECT id FROM obligations WHERE artifact_id = $1", uuid.UUID(artifact_id))
    await _accept(family, first["id"])

    async with pool().acquire() as conn, conn.transaction():
        await reminders.schedule(conn, dt.date.today())
    assert await database.fetchval("SELECT count(*) FROM reminders WHERE status = 'pending'") > 0

    await service.save(household_id, uuid.UUID(artifact_id), _read(("Return the slip", SEVENTEENTH)))
    async with pool().acquire() as conn, conn.transaction():
        await reminders.schedule(conn, dt.date.today())
    stale = await database.fetch(
        "SELECT r.status FROM reminders r JOIN obligations o ON o.id = r.obligation_id "
        "WHERE o.superseded_at IS NOT NULL")
    assert stale and all(r["status"] == "cancelled" for r in stale)
