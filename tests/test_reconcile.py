"""Reconciliation: a revised notice joined to the one it revises."""
import datetime as dt
import uuid

from familyos import reconcile, reminders
from familyos.db import pool
from familyos.settings import settings
from tests.conftest import pdf
from tests.test_reminders import _sweep


async def _claim(database, family, *, artifact_id, kind, title, applies_to=None, amends=None,
                 change=None, date=None):
    extraction_id, claim_id = uuid.uuid4(), uuid.uuid4()
    household_id = uuid.UUID(family.household["id"])
    await database.execute(
        "INSERT INTO extractions (id, household_id, artifact_id, extractor, prompt_version, parser_version, "
        "actionable, page_count) VALUES ($1, $2, $3, 'rules', 'v1', 'v1', TRUE, 1)",
        extraction_id, household_id, uuid.UUID(artifact_id))
    await database.execute(
        "INSERT INTO claims (id, extraction_id, household_id, artifact_id, ordinal, kind, title, applies_to, "
        "amends, change, date, quote, confidence) VALUES ($1, $2, $3, $4, 0, $5, $6, $7, $8, $9, $10, 'words', 0.9)",
        claim_id, extraction_id, household_id, uuid.UUID(artifact_id), kind, title, applies_to, amends, change, date)
    return claim_id


async def _obligation_for(database, family, *, artifact_id, claim_id, due, title="Do the thing"):
    """An obligation on a claim that already exists. Unlike the reminders
    helper this makes no extraction: one artifact has only one current one."""
    obligation_id = uuid.uuid4()
    await database.execute(
        "INSERT INTO obligations (id, household_id, artifact_id, claim_id, kind, action, title, due_date, status) "
        "VALUES ($1, $2, $3, $4, 'task', 'sign', $5, $6, 'accepted')",
        obligation_id, uuid.UUID(family.household["id"]), uuid.UUID(artifact_id), claim_id, title, due)
    return obligation_id


async def _propose(family, artifact_id):
    async with pool().acquire() as conn, conn.transaction():
        return await reconcile.propose(conn, uuid.UUID(family.household["id"]), uuid.UUID(artifact_id))


async def test_a_revised_notice_is_matched_to_the_one_it_revises(family, database):
    old = (await family.upload("amma", pdf("old"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id=old, kind="event", title="Annual day celebration",
                 applies_to="Classes I to V", date=dt.date.today() + dt.timedelta(days=30))

    new = (await family.upload("amma", pdf("new"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id=new, kind="amendment", title="Annual day celebration postponed",
                 applies_to="Classes I to V", amends="Annual day celebration",
                 change="now on 21 November", date=dt.date.today() + dt.timedelta(days=37))

    assert await _propose(family, new) == 1
    row = dict(await database.fetchrow("SELECT * FROM amendments"))
    assert row["status"] == "proposed"
    assert row["amends_artifact_id"] == uuid.UUID(old)
    assert row["score"] >= reconcile.MIN_SCORE
    assert "same group" in row["matched_on"]


async def test_an_unrelated_notice_is_not_matched(family, database):
    old = (await family.upload("amma", pdf("x"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id=old, kind="deadline", title="Library books to be returned",
                 date=dt.date.today() + dt.timedelta(days=5))

    new = (await family.upload("amma", pdf("y"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id=new, kind="amendment", title="Swimming gala timings revised",
                 amends="Swimming gala")

    assert await _propose(family, new) == 0
    assert await database.fetchval("SELECT count(*) FROM amendments") == 0


async def test_proposing_twice_makes_one_link(family, database):
    old = (await family.upload("amma", pdf("o"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id=old, kind="event", title="Sports day heats",
                 date=dt.date.today() + dt.timedelta(days=10))
    new = (await family.upload("amma", pdf("n"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id=new, kind="amendment", title="Sports day heats moved",
                 amends="Sports day heats")

    assert await _propose(family, new) == 1
    assert await _propose(family, new) == 0
    assert await database.fetchval("SELECT count(*) FROM amendments") == 1


async def test_confirming_supersedes_the_old_obligation_and_stops_its_reminders(family, database, monkeypatch):
    monkeypatch.setattr(settings, "reminder_channel", "log")
    monkeypatch.setattr(settings, "reminder_lead_days", "7,1,0")

    old = (await family.upload("amma", pdf("p"), visibility="shared")).json()["artifact"]["id"]
    # Three days out, so the seven-day lead has already come round and one
    # reminder has genuinely gone before the amendment is confirmed.
    old_claim = await _claim(database, family, artifact_id=old, kind="deadline",
                             title="Annual day costume to be sent", applies_to="Class II",
                             date=dt.date.today() + dt.timedelta(days=3))
    obligation_id = await _obligation_for(database, family, artifact_id=old, claim_id=old_claim,
                                          due=dt.date.today() + dt.timedelta(days=3),
                                          title="Send the annual day costume")

    new = (await family.upload("amma", pdf("q"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id=new, kind="amendment",
                 title="Annual day costume no longer needed", applies_to="Class II",
                 amends="Annual day costume to be sent", change="costumes provided by the school")
    assert await _propose(family, new) == 1

    # Before confirmation both stand, and the reminder that goes out says so.
    await _sweep()
    sent = await database.fetch("SELECT status FROM reminders WHERE obligation_id = $1", obligation_id)
    assert any(r["status"] == "sent" for r in sent)

    amendment_id = await database.fetchval("SELECT id FROM amendments")
    r = await family.client.post(f"/v1/amendments/{amendment_id}/decision", headers=family.h("amma"),
                                 json={"status": "confirmed"})
    assert r.status_code == 200, r.text
    assert r.json()["obligations_superseded"] == 1

    row = dict(await database.fetchrow("SELECT superseded_at, superseded_by_artifact_id FROM obligations WHERE id = $1",
                                       obligation_id))
    assert row["superseded_at"] is not None
    assert row["superseded_by_artifact_id"] == uuid.UUID(new)
    left = await database.fetchval(
        "SELECT count(*) FROM reminders WHERE obligation_id = $1 AND status = 'pending'", obligation_id)
    assert left == 0

    # And the sweep does not bring them back.
    await _sweep()
    assert await database.fetchval(
        "SELECT count(*) FROM reminders WHERE obligation_id = $1 AND status = 'pending'", obligation_id) == 0


async def test_rejecting_leaves_both_notices_standing(family, database):
    old = (await family.upload("amma", pdf("r"), visibility="shared")).json()["artifact"]["id"]
    old_claim = await _claim(database, family, artifact_id=old, kind="deadline", title="Fee instalment due",
                             date=dt.date.today() + dt.timedelta(days=9))
    obligation_id = await _obligation_for(database, family, artifact_id=old, claim_id=old_claim,
                                          due=dt.date.today() + dt.timedelta(days=9),
                                          title="Pay the fee instalment")

    new = (await family.upload("amma", pdf("s"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id=new, kind="amendment", title="Fee instalment due date revised",
                 amends="Fee instalment due")
    await _propose(family, new)

    amendment_id = await database.fetchval("SELECT id FROM amendments")
    r = await family.client.post(f"/v1/amendments/{amendment_id}/decision", headers=family.h("amma"),
                                 json={"status": "rejected"})
    assert r.status_code == 200 and r.json()["obligations_superseded"] == 0
    assert await database.fetchval("SELECT superseded_at FROM obligations WHERE id = $1", obligation_id) is None
    # Deciding it again is refused.
    again = await family.client.post(f"/v1/amendments/{amendment_id}/decision", headers=family.h("amma"),
                                     json={"status": "confirmed"})
    assert again.status_code == 403


async def test_a_member_who_cannot_see_both_notices_is_not_shown_the_link(family, database):
    """Confirming says two notices are the same thing, so it needs sight of
    both. Appa's private notice must not surface through Amma's list."""
    old = (await family.upload("appa", pdf("t"), visibility="private")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id=old, kind="event", title="Chess club trials",
                 date=dt.date.today() + dt.timedelta(days=6))
    new = (await family.upload("appa", pdf("u"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id=new, kind="amendment", title="Chess club trials rescheduled",
                 amends="Chess club trials")
    assert await _propose(family, new) == 1

    appa = (await family.client.get("/v1/amendments", headers=family.h("appa"))).json()
    amma = (await family.client.get("/v1/amendments", headers=family.h("amma"))).json()
    assert len(appa) == 1
    assert amma == []

    amendment_id = appa[0]["id"]
    denied = await family.client.post(f"/v1/amendments/{amendment_id}/decision", headers=family.h("amma"),
                                      json={"status": "confirmed"})
    assert denied.status_code == 404


async def test_a_contested_reminder_says_so(family, database, monkeypatch):
    monkeypatch.setattr(settings, "reminder_channel", "log")
    row = {
        "id": uuid.uuid4(), "member_id": uuid.uuid4(), "reason": "due", "action": "sign",
        "title": "Send the costume", "due_date": dt.date.today() + dt.timedelta(days=1),
        "subject_name": None, "received_at": dt.datetime.now(dt.UTC), "contested": True,
    }
    _, body = reminders.compose(row)
    assert "A later notice looks like it changes this" in body
    row["contested"] = False
    _, plain = reminders.compose(row)
    assert "later notice" not in plain


def test_scoring_ignores_the_words_every_notice_uses():
    amendment = {"title": "Revised: annual day timings", "amends": "the annual day circular", "applies_to": "Class II"}
    good = {"title": "Annual day celebration", "applies_to": "Class II", "kind": "event",
            "date": dt.date.today()}
    bad = {"title": "Dental camp consent", "applies_to": "Class II", "kind": "deadline",
           "date": dt.date.today()}
    good_score, why = reconcile.score(amendment, good)
    bad_score, _ = reconcile.score(amendment, bad)
    assert good_score >= reconcile.MIN_SCORE and good_score > bad_score
    assert bad_score == 0.0            # "class" and "camp" share nothing real
    assert "same group" in why
    # "revised", "circular" and "the" carry no signal.
    assert reconcile.words("Revised circular: the annual day") == {"annual", "day"}
