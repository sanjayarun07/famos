"""Reminders: who gets told, when, and never twice."""
import datetime as dt
import uuid

import pytest

from familyos import jobs, reminders
from familyos.db import pool
from familyos.settings import settings
from tests.conftest import pdf


async def _obligation(database, family, *, artifact_id, due, status="accepted", title="Return the slip",
                      action="sign"):
    """Put an obligation straight in: extraction has its own tests, and these
    are about what happens to a dated obligation once it exists."""
    extraction_id, claim_id, obligation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await database.execute(
        "INSERT INTO extractions (id, household_id, artifact_id, extractor, prompt_version, parser_version, "
        "actionable, page_count) VALUES ($1, $2, $3, 'rules', 'v1', 'v1', TRUE, 1)",
        extraction_id, uuid.UUID(family.household["id"]), uuid.UUID(artifact_id))
    await database.execute(
        "INSERT INTO claims (id, extraction_id, household_id, artifact_id, ordinal, kind, title, quote, confidence) "
        "VALUES ($1, $2, $3, $4, 0, 'deadline', $5, 'the words', 0.9)",
        claim_id, extraction_id, uuid.UUID(family.household["id"]), uuid.UUID(artifact_id), title)
    await database.execute(
        "INSERT INTO obligations (id, household_id, artifact_id, claim_id, kind, action, title, due_date, status) "
        "VALUES ($1, $2, $3, $4, 'task', $5, $6, $7, $8)",
        obligation_id, uuid.UUID(family.household["id"]), uuid.UUID(artifact_id), claim_id, action, title,
        due, status)
    return obligation_id


async def _sweep(now=None):
    now = now or dt.datetime.now(dt.UTC)
    async with pool().acquire() as conn, conn.transaction():
        made = await reminders.schedule(conn, now.date())
    sent = await reminders.send_due(now)
    return made, sent


@pytest.fixture(autouse=True)
def log_channel(monkeypatch):
    monkeypatch.setattr(settings, "reminder_channel", "log")
    monkeypatch.setattr(settings, "reminder_lead_days", "7,1,0")


async def test_a_shared_notice_reminds_everyone_who_can_see_it(family, database):
    r = await family.upload("amma", pdf("a"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id, due=dt.date.today() + dt.timedelta(days=1))

    await _sweep()
    told = {row["display_name"] for row in await database.fetch(
        "SELECT m.display_name FROM reminders r JOIN members m ON m.id = r.member_id")}
    # The three who sign in, and never the children.
    assert told == {"Amma", "Appa", "Paati"}


async def test_a_private_notice_reminds_only_its_sender(family, database):
    """The visibility rule holds here too: a reminder must not tell the
    household about a notice it cannot see."""
    r = await family.upload("amma", pdf("b"), visibility="private")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id, due=dt.date.today() + dt.timedelta(days=1))

    await _sweep()
    told = {row["display_name"] for row in await database.fetch(
        "SELECT m.display_name FROM reminders r JOIN members m ON m.id = r.member_id")}
    assert told == {"Amma"}


async def test_nothing_is_sent_twice_however_often_the_sweep_runs(family, database):
    r = await family.upload("amma", pdf("c"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id, due=dt.date.today())

    first_made, first_sent = await _sweep()
    second_made, second_sent = await _sweep()
    third_made, third_sent = await _sweep()

    assert first_sent["sent"] > 0
    assert second_sent["sent"] == 0 and third_sent["sent"] == 0
    assert second_made["due"] == 0 and third_made["due"] == 0
    assert await database.fetchval("SELECT count(*) FROM reminders WHERE status = 'sent'") == first_sent["sent"]


async def test_only_the_lead_times_that_have_arrived_are_sent(family, database):
    r = await family.upload("amma", pdf("d"), visibility="private")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id, due=dt.date.today() + dt.timedelta(days=7))

    await _sweep()
    rows = {row["lead_days"]: row["status"] for row in await database.fetch(
        "SELECT lead_days, status FROM reminders WHERE reason = 'due' ORDER BY lead_days")}
    # Seven days out: only the seven-day one is due now.
    assert rows == {7: "sent", 1: "pending", 0: "pending"}


async def test_an_undecided_proposal_is_nudged_and_stops_once_decided(family, database):
    r = await family.upload("amma", pdf("e"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    obligation_id = await _obligation(database, family, artifact_id=artifact_id,
                                      due=dt.date.today() + dt.timedelta(days=1), status="proposed")
    await _sweep()
    assert await database.fetchval("SELECT count(*) FROM reminders WHERE reason = 'undecided'") > 0
    assert await database.fetchval("SELECT count(*) FROM reminders WHERE reason = 'due'") == 0

    # Someone dismisses it; the pending nudges are called off.
    await database.execute("UPDATE obligations SET status = 'dismissed' WHERE id = $1", obligation_id)
    await _sweep()
    left = await database.fetchval(
        "SELECT count(*) FROM reminders WHERE reason = 'undecided' AND status = 'pending'")
    assert left == 0


async def test_a_member_sees_their_own_reminders_over_the_api(family, database):
    r = await family.upload("appa", pdf("f"), visibility="private")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id, due=dt.date.today() + dt.timedelta(days=1))
    await _sweep()

    appa = (await family.client.get("/v1/reminders", headers=family.h("appa"))).json()
    amma = (await family.client.get("/v1/reminders", headers=family.h("amma"))).json()
    assert len(appa) == 3 and all(x["title"] == "Return the slip" for x in appa)
    assert amma == []          # Amma cannot see Appa's private notice, so is told nothing about it


async def test_the_sweep_reschedules_itself(family, database):
    reminders.register()
    job = await reminders.ensure_scheduled()
    assert job is not None
    # A second call does not make a rival sweep.
    assert await reminders.ensure_scheduled() is None

    settled = await jobs.attach(job["id"])
    assert settled["status"] == "scheduled", settled
    assert settled["next_run_at"] is not None


async def test_erasing_the_household_takes_its_reminders(family, database):
    r = await family.upload("amma", pdf("g"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id, due=dt.date.today())
    await _sweep()
    assert await database.fetchval("SELECT count(*) FROM reminders") > 0

    await database.execute("DELETE FROM households WHERE id = $1", uuid.UUID(family.household["id"]))
    assert await database.fetchval("SELECT count(*) FROM reminders") == 0


def test_lead_days_are_read_nearest_last(monkeypatch):
    monkeypatch.setattr(settings, "reminder_lead_days", "0, 3,14, junk, -2, 3")
    assert reminders.lead_days() == [14, 3, 0]
    monkeypatch.setattr(settings, "reminder_lead_days", "")
    assert reminders.lead_days() == [0]


def test_the_wording_says_what_and_when_but_not_the_notice():
    row = {
        "id": uuid.uuid4(), "member_id": uuid.uuid4(), "reason": "due", "action": "sign",
        "title": "Return the trip consent form", "due_date": dt.date.today() + dt.timedelta(days=1),
        "subject_name": "Meera", "received_at": dt.datetime.now(dt.UTC),
    }
    subject, body = reminders.compose(row)
    assert "needs signing" in subject and "Return the trip consent form" in subject
    assert "due tomorrow" in body and "For: Meera" in body

    row["reason"] = "undecided"
    subject, body = reminders.compose(row)
    assert subject.startswith("Still to decide")
    assert "accepted or dismissed" in body
