"""The same notice arriving twice, and notices heard rather than issued.

A parents' group makes both the normal case: three people relay one circular,
and what they relay is a retelling rather than the school's own words.
"""
import datetime as dt
import uuid

from familyos import reconcile
from familyos.db import pool
from familyos.extraction import service
from familyos.extraction.claims import Claim, Extraction, ExtractorInfo
from tests.conftest import pdf

# Grounded, because an ungrounded claim never becomes an obligation and these
# tests are about what happens to obligations.
GROUNDED = {"page": 1, "start": 0, "end": 16, "boxes": [[60.0, 78.0, 300.0, 93.0]], "match": "exact"}


def _read(title="Return the trip consent slip", when=None, confidence=0.9):
    return Extraction(
        extractor=ExtractorInfo(name="openai", model="openai/gpt-5.6-sol",
                                prompt_version="extract-v1", parser_version="p1"),
        reference_date=dt.date.today(), actionable=True, page_count=1,
        claims=[Claim(kind="deadline", title=title, quote="Return the slip", location=GROUNDED,
                      date=when or dt.date.today() + dt.timedelta(days=5), confidence=confidence)])


async def _forwarded(database, artifact_id):
    await database.execute(
        "UPDATE input_artifacts SET source = jsonb_set(COALESCE(source, '{}'::jsonb), '{forwarded}', 'true') "
        "WHERE id = $1", uuid.UUID(artifact_id))


# ----------------------------------------------------------------------------
# the same notice, from two parents
# ----------------------------------------------------------------------------

async def test_the_same_bytes_from_two_members_propose_tasks_once(family, database):
    """Storage has always deduplicated the bytes. What it did not do was stop
    the family being told twice."""
    same = pdf("one circular")
    first = (await family.upload("amma", same, visibility="shared")).json()["artifact"]["id"]
    second = (await family.upload("appa", same, visibility="shared")).json()["artifact"]["id"]
    assert first != second
    assert await database.fetchval("SELECT count(*) FROM blobs") == 1

    await service.save(uuid.UUID(family.household["id"]), uuid.UUID(first), _read())
    counts = await service.save(uuid.UUID(family.household["id"]), uuid.UUID(second), _read())

    assert counts["duplicate_of"] == first
    assert counts["obligations_superseded"] == 1

    live = await database.fetch("SELECT artifact_id FROM obligations WHERE superseded_at IS NULL")
    assert [r["artifact_id"] for r in live] == [uuid.UUID(first)]
    # The later copy is kept, not deleted: two parents did send it.
    assert await database.fetchval("SELECT count(*) FROM input_artifacts") == 2
    assert await database.fetchval(
        "SELECT superseded_by_artifact_id FROM obligations WHERE artifact_id = $1", uuid.UUID(second)
    ) == uuid.UUID(first)


async def test_the_first_copy_is_the_one_kept(family, database):
    """Whoever sent it first is canonical, whichever order they are read in."""
    same = pdf("circular two")
    first = (await family.upload("amma", same, visibility="shared")).json()["artifact"]["id"]
    second = (await family.upload("appa", same, visibility="shared")).json()["artifact"]["id"]
    # Read the later one first.
    await service.save(uuid.UUID(family.household["id"]), uuid.UUID(second), _read())
    await service.save(uuid.UUID(family.household["id"]), uuid.UUID(first), _read())

    live = await database.fetch("SELECT artifact_id FROM obligations WHERE superseded_at IS NULL")
    assert [r["artifact_id"] for r in live] == [uuid.UUID(first)]


async def test_different_notices_are_left_alone(family, database):
    a = (await family.upload("amma", pdf("alpha"), visibility="shared")).json()["artifact"]["id"]
    b = (await family.upload("amma", pdf("beta"), visibility="shared")).json()["artifact"]["id"]
    await service.save(uuid.UUID(family.household["id"]), uuid.UUID(a), _read("Sports day"))
    counts = await service.save(uuid.UUID(family.household["id"]), uuid.UUID(b), _read("Fee reminder"))
    assert counts["duplicate_of"] is None
    assert await database.fetchval("SELECT count(*) FROM obligations WHERE superseded_at IS NULL") == 2


async def test_a_superseded_duplicate_stops_reminding(family, database, monkeypatch):
    from familyos.settings import settings
    monkeypatch.setattr(settings, "reminder_channel", "log")
    monkeypatch.setattr(settings, "reminder_lead_days", "7,1,0")
    from tests.test_reminders import _sweep

    same = pdf("circular three")
    first = (await family.upload("amma", same, visibility="shared")).json()["artifact"]["id"]
    second = (await family.upload("appa", same, visibility="shared")).json()["artifact"]["id"]
    await service.save(uuid.UUID(family.household["id"]), uuid.UUID(first), _read())
    await service.save(uuid.UUID(family.household["id"]), uuid.UUID(second), _read())

    await _sweep()
    told = await database.fetch(
        "SELECT o.artifact_id, count(*) AS n FROM reminders r JOIN obligations o ON o.id = r.obligation_id "
        "GROUP BY o.artifact_id")
    # Only the first copy reminds; the duplicate was superseded before the sweep.
    assert [r["artifact_id"] for r in told] == [uuid.UUID(first)]


# ----------------------------------------------------------------------------
# heard, not issued
# ----------------------------------------------------------------------------

async def test_a_forwarded_notice_is_held_less_confidently(family, database):
    plain = (await family.upload("amma", pdf("issued"), visibility="shared")).json()["artifact"]["id"]
    relayed = (await family.upload("amma", pdf("relayed"), visibility="shared")).json()["artifact"]["id"]
    await _forwarded(database, relayed)

    straight = await service.save(uuid.UUID(family.household["id"]), uuid.UUID(plain), _read(confidence=0.9))
    second_hand = await service.save(uuid.UUID(family.household["id"]), uuid.UUID(relayed),
                                     _read(title="Return the slip, per the group", confidence=0.9))

    assert straight["hearsay"] is False and second_hand["hearsay"] is True
    issued = await database.fetchval("SELECT confidence FROM claims WHERE artifact_id = $1", uuid.UUID(plain))
    heard = await database.fetchval("SELECT confidence FROM claims WHERE artifact_id = $1", uuid.UUID(relayed))
    # confidence is REAL, so compare with a tolerance rather than exactly.
    assert abs(issued - 0.9) < 0.001
    assert abs(heard - 0.9 * service.HEARSAY_PENALTY) < 0.001
    assert heard < issued


async def test_hearsay_still_becomes_a_task(family, database):
    """Held less confidently is not the same as ignored: the parents' group is
    usually how a family hears anything first."""
    relayed = (await family.upload("amma", pdf("group relay"), visibility="shared")).json()["artifact"]["id"]
    await _forwarded(database, relayed)
    await service.save(uuid.UUID(family.household["id"]), uuid.UUID(relayed), _read())
    assert await database.fetchval("SELECT count(*) FROM obligations WHERE superseded_at IS NULL") == 1


async def test_merging_is_certain_enough_to_need_nobody(family, database):
    """Matching two notices by their words is a guess and is proposed; matching
    them by SHA-256 is not, so no amendment row is created for it."""
    same = pdf("certain")
    first = (await family.upload("amma", same, visibility="shared")).json()["artifact"]["id"]
    second = (await family.upload("appa", same, visibility="shared")).json()["artifact"]["id"]
    await service.save(uuid.UUID(family.household["id"]), uuid.UUID(first), _read())
    await service.save(uuid.UUID(family.household["id"]), uuid.UUID(second), _read())
    assert await database.fetchval("SELECT count(*) FROM amendments") == 0
    assert await database.fetchval(
        "SELECT count(*) FROM audit_events WHERE action = 'artifact.duplicate_merged'") == 1


async def test_merge_is_safe_to_repeat(family, database):
    same = pdf("repeatable")
    first = (await family.upload("amma", same, visibility="shared")).json()["artifact"]["id"]
    second = (await family.upload("appa", same, visibility="shared")).json()["artifact"]["id"]
    await service.save(uuid.UUID(family.household["id"]), uuid.UUID(first), _read())
    async with pool().acquire() as conn, conn.transaction():
        once = await reconcile.merge_exact_duplicate(conn, uuid.UUID(family.household["id"]), uuid.UUID(second))
        twice = await reconcile.merge_exact_duplicate(conn, uuid.UUID(family.household["id"]), uuid.UUID(second))
    assert once["first_artifact_id"] == first
    # Nothing left to supersede the second time.
    assert twice["obligations_superseded"] == 0
