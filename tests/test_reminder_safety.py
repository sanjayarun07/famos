"""Three ways a reminder used to go wrong.

A reminder is a sentence about a notice, sent to somebody, about a date. Each
of those three can be wrong after the row is made: the notice can stop being
theirs to read, the sentence can be sent twice, and the date can stop being
what the notice says.
"""
import datetime as dt
import uuid

import pytest

from familyos import reminders
from familyos.db import pool
from familyos.settings import settings
from tests.conftest import pdf

TOMORROW = dt.date.today() + dt.timedelta(days=1)


@pytest.fixture(autouse=True)
def log_channel(monkeypatch):
    monkeypatch.setattr(settings, "reminder_channel", "log")
    monkeypatch.setattr(settings, "reminder_lead_days", "1")


async def _obligation(database, family, *, artifact_id, due=TOMORROW, status="accepted", title="Return the slip"):
    household_id = uuid.UUID(family.household["id"])
    extraction_id, claim_id, obligation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await database.execute(
        "INSERT INTO extractions (id, household_id, artifact_id, extractor, prompt_version, parser_version, "
        "actionable, page_count) VALUES ($1, $2, $3, 'rules', 'v1', 'v1', TRUE, 1)",
        extraction_id, household_id, uuid.UUID(artifact_id))
    await database.execute(
        "INSERT INTO claims (id, extraction_id, household_id, artifact_id, ordinal, kind, title, quote, confidence) "
        "VALUES ($1, $2, $3, $4, 0, 'deadline', $5, 'the words', 0.9)",
        claim_id, extraction_id, household_id, uuid.UUID(artifact_id), title)
    await database.execute(
        "INSERT INTO obligations (id, household_id, artifact_id, claim_id, kind, action, title, due_date, status) "
        "VALUES ($1, $2, $3, $4, 'task', 'sign', $5, $6, $7)",
        obligation_id, household_id, uuid.UUID(artifact_id), claim_id, title, due, status)
    return obligation_id


async def _sweep(now=None):
    now = now or dt.datetime.now(dt.UTC)
    async with pool().acquire() as conn, conn.transaction():
        made = await reminders.schedule(conn, now.date())
    sent = await reminders.send_due(now)
    return made, sent


async def _told(database, status=None):
    rows = await database.fetch(
        "SELECT m.display_name, r.status FROM reminders r JOIN members m ON m.id = r.member_id"
        + (" WHERE r.status = $1" if status else ""), *( [status] if status else []))
    return {r["display_name"] for r in rows}


# ----------------------------------------------------------------------------
# making a notice private
# ----------------------------------------------------------------------------

async def test_making_a_notice_private_stops_reminding_everyone_else(family, database):
    """Recipients are chosen when the row is made. Narrowing the notice
    afterwards has to narrow the reminders too, or the reminder tells the
    household about a notice it can no longer open."""
    r = await family.upload("amma", pdf("p1"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id)
    async with pool().acquire() as conn, conn.transaction():
        await reminders.schedule(conn, dt.date.today())
    assert await _told(database) == {"Amma", "Appa", "Paati"}

    got = await family.client.patch(f"/v1/artifacts/{artifact_id}", headers=family.h("amma"),
                                    json={"visibility": "private"})
    assert got.status_code == 200, got.text

    await _sweep()
    # Only the sender is still told; the others' rows were cancelled, not sent.
    assert await _told(database, "sent") == {"Amma"}
    assert await _told(database, "cancelled") == {"Appa", "Paati"}


async def test_delivery_rechecks_visibility_even_without_a_sweep(family, database):
    """The cancel sweep tidies the queue, but it is not the guarantee: a
    notice made private between two sweeps must not be delivered either."""
    r = await family.upload("amma", pdf("p2"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id)
    async with pool().acquire() as conn, conn.transaction():
        await reminders.schedule(conn, dt.date.today())

    await database.execute("UPDATE input_artifacts SET visibility = 'private' WHERE id = $1",
                           uuid.UUID(artifact_id))
    # No schedule() call here, so nothing has been cancelled.
    sent = await reminders.send_due(dt.datetime.now(dt.UTC))
    assert sent["sent"] == 1
    assert await _told(database, "sent") == {"Amma"}


async def test_a_private_notices_reminders_leave_the_others_lists(family, database):
    """Reading is the same rule: a row for a notice the member may no longer
    see is not shown to them, sent ones included."""
    r = await family.upload("amma", pdf("p3"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id)
    await _sweep()
    assert len((await family.client.get("/v1/reminders", headers=family.h("appa"))).json()) == 1

    await database.execute("UPDATE input_artifacts SET visibility = 'private' WHERE id = $1",
                           uuid.UUID(artifact_id))
    assert (await family.client.get("/v1/reminders", headers=family.h("appa"))).json() == []
    assert len((await family.client.get("/v1/reminders", headers=family.h("amma"))).json()) == 1


# ----------------------------------------------------------------------------
# sending once
# ----------------------------------------------------------------------------

async def test_two_workers_do_not_send_the_same_reminder(family, database):
    """Both sweeps run against the same due row. One claims it, the other
    steps over it."""
    r = await family.upload("amma", pdf("s1"), visibility="private")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id)
    async with pool().acquire() as conn, conn.transaction():
        await reminders.schedule(conn, dt.date.today())

    now = dt.datetime.now(dt.UTC)
    import asyncio
    first, second = await asyncio.gather(reminders.send_due(now), reminders.send_due(now))
    assert first["sent"] + second["sent"] == 1
    assert await database.fetchval("SELECT count(*) FROM reminders WHERE status = 'sent'") == 1


async def test_a_claim_is_not_handed_out_again_until_it_goes_stale(family, database):
    """A row being sent is not offered to the next sweep. A row whose worker
    died is, once the claim is older than CLAIM_TTL -- never sent at all is
    worse than sent twice."""
    r = await family.upload("amma", pdf("s2"), visibility="private")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id)
    async with pool().acquire() as conn, conn.transaction():
        await reminders.schedule(conn, dt.date.today())

    now = dt.datetime.now(dt.UTC)
    # Mimic a worker that claimed the row and then died.
    await database.execute("UPDATE reminders SET status = 'sending', claimed_at = $1, attempts = 1", now)
    assert (await reminders.send_due(now))["sent"] == 0

    later = now + reminders.CLAIM_TTL + dt.timedelta(seconds=1)
    assert (await reminders.send_due(later))["sent"] == 1


async def test_a_failed_send_counts_one_attempt_not_two(family, database):
    """The claim counts the attempt. The failure branch must not count it
    again, or reminder_max_attempts is reached in half the tries."""
    r = await family.upload("amma", pdf("s3"), visibility="private")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id)
    async with pool().acquire() as conn, conn.transaction():
        await reminders.schedule(conn, dt.date.today())

    async def explode(row, subject, body):
        raise RuntimeError("the channel is down")

    reminders.CHANNELS["log"] = explode
    try:
        assert (await reminders.send_due(dt.datetime.now(dt.UTC)))["failed"] == 1
    finally:
        reminders.CHANNELS["log"] = reminders._send_log
    row = await database.fetchrow("SELECT status, attempts, claimed_at FROM reminders")
    assert (row["status"], row["attempts"], row["claimed_at"]) == ("pending", 1, None)
