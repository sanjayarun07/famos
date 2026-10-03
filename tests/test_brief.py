"""The brief: the few things that matter today, worst first."""
import datetime as dt
import uuid

import pytest

from familyos import brief
from familyos.settings import settings

from tests.conftest import pdf

TODAY = dt.date.today()


@pytest.fixture(autouse=True)
def lead_days(monkeypatch):
    monkeypatch.setattr(settings, "reminder_lead_days", "7,1,0")


async def _claim(database, family, artifact_id, *, title="Return the slip", date=None):
    """A claim, straight in: extraction has its own tests and these are about
    what the brief makes of an obligation once it exists."""
    household_id = uuid.UUID(family.household["id"])
    extraction_id = await database.fetchval(
        "SELECT id FROM extractions WHERE artifact_id = $1", uuid.UUID(artifact_id))
    if extraction_id is None:
        extraction_id = uuid.uuid4()
        await database.execute(
            "INSERT INTO extractions (id, household_id, artifact_id, extractor, prompt_version, parser_version, "
            "actionable, page_count) VALUES ($1, $2, $3, 'rules', 'v1', 'v1', TRUE, 1)",
            extraction_id, household_id, uuid.UUID(artifact_id))
    claim_id = uuid.uuid4()
    ordinal = await database.fetchval(
        "SELECT COALESCE(MAX(ordinal) + 1, 0) FROM claims WHERE extraction_id = $1", extraction_id)
    await database.execute(
        "INSERT INTO claims (id, extraction_id, household_id, artifact_id, ordinal, kind, title, date, quote, "
        "confidence) VALUES ($1, $2, $3, $4, $5, 'deadline', $6, $7, 'the words', 0.9)",
        claim_id, extraction_id, household_id, uuid.UUID(artifact_id), ordinal, title, date)
    return claim_id


async def _obligation(database, family, *, artifact_id, due, status="accepted", title="Return the slip",
                      action="sign", superseded=False):
    household_id = uuid.UUID(family.household["id"])
    claim_id = await _claim(database, family, artifact_id, title=title, date=due)
    obligation_id = uuid.uuid4()
    await database.execute(
        "INSERT INTO obligations (id, household_id, artifact_id, claim_id, kind, action, title, due_date, status, "
        "superseded_at) VALUES ($1, $2, $3, $4, 'task', $5, $6, $7, $8, $9)",
        obligation_id, household_id, uuid.UUID(artifact_id), claim_id, action, title, due, status,
        dt.datetime.now(dt.UTC) if superseded else None)
    return obligation_id


async def _brief(family, who="amma", **params):
    r = await family.client.get("/v1/brief", headers=family.h(who), params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def test_worst_first(family, database):
    """A thing nobody decided outranks a thing the family agreed to, and a
    date gone outranks a date coming."""
    r = await family.upload("amma", pdf("a"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    for title, due, status in (
            ("Coming up", TODAY + dt.timedelta(days=5), "accepted"),
            ("Still to decide", TODAY + dt.timedelta(days=5), "proposed"),
            ("Falls today", TODAY, "accepted"),
            ("Agreed and gone", TODAY - dt.timedelta(days=2), "accepted"),
            ("Nobody decided and gone", TODAY - dt.timedelta(days=2), "proposed")):
        await _obligation(database, family, artifact_id=artifact_id, due=due, status=status, title=title)

    body = await _brief(family, limit=-1)
    assert [i["reason"] for i in body["items"]] == ["missed", "overdue", "today", "undecided", "soon"]
    assert [i["title"] for i in body["items"]][0] == "Nobody decided and gone"


async def test_three_things_and_an_honest_count(family, database):
    r = await family.upload("amma", pdf("b"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    for n in range(6):
        await _obligation(database, family, artifact_id=artifact_id, due=TODAY, title=f"Thing {n}")

    body = await _brief(family)
    assert len(body["items"]) == brief.DEFAULT_LIMIT == 3
    # counts is everything found, so "and 3 more" can be said truthfully.
    assert body["counts"]["today"] == 6
    assert body["more"] == 3


async def test_a_private_notice_reaches_only_its_senders_brief(family, database):
    """The visibility rule again: the brief is a read like any other."""
    r = await family.upload("amma", pdf("c"), visibility="private")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id, due=TODAY, title="Amma's own")

    assert [i["title"] for i in (await _brief(family, "amma"))["items"]] == ["Amma's own"]
    assert (await _brief(family, "appa"))["items"] == []


async def test_undated_and_superseded_and_stale_stay_out(family, database):
    """The brief is about dates that are about to bite. An undated obligation
    has nothing to be late for, a superseded one was replaced, and six weeks
    ago is history -- all three are still in the obligations list."""
    r = await family.upload("amma", pdf("d"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id, due=None, title="No date")
    await _obligation(database, family, artifact_id=artifact_id, due=TODAY, title="Replaced", superseded=True)
    await _obligation(database, family, artifact_id=artifact_id, title="Long gone",
                      due=TODAY - dt.timedelta(days=brief.STALE_DAYS + 1))
    await _obligation(database, family, artifact_id=artifact_id, title="Beyond the window",
                      due=TODAY + dt.timedelta(days=brief.window() + 1))

    assert (await _brief(family, limit=-1))["items"] == []


async def test_coming_up_means_what_it_means_to_the_reminders(family, database):
    """One source of "near": the widest reminder lead time. If these two
    disagreed, a family would be reminded about something the brief never
    mentioned, or the reverse."""
    r = await family.upload("amma", pdf("e"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id, title="On the edge",
                      due=TODAY + dt.timedelta(days=brief.window()))

    body = await _brief(family, limit=-1)
    assert [i["title"] for i in body["items"]] == ["On the edge"]
    assert body["items"][0]["when"] == f"in {brief.window()} days"


async def test_a_pending_revision_is_worth_saying(family, database):
    """A later notice looks like it moves the date and nobody has confirmed
    that. Acting on the old date is the risk, so the brief says so."""
    household_id = uuid.UUID(family.household["id"])
    first = (await family.upload("amma", pdf("f1"), visibility="shared")).json()["artifact"]["id"]
    second = (await family.upload("amma", pdf("f2"), visibility="shared")).json()["artifact"]["id"]
    old = await _claim(database, family, first, title="Sports day", date=TODAY + dt.timedelta(days=3))
    new = await _claim(database, family, second, title="Sports day moved", date=TODAY + dt.timedelta(days=10))
    await database.execute(
        "INSERT INTO amendments (id, household_id, artifact_id, claim_id, amends_artifact_id, amends_claim_id, "
        "score, matched_on) VALUES ($1, $2, $3, $4, $5, $6, 0.9, 'title')",
        uuid.uuid4(), household_id, uuid.UUID(second), new, uuid.UUID(first), old)

    body = await _brief(family, limit=-1)
    item = next(i for i in body["items"] if i["reason"] == "amendment")
    assert item["title"] == "Sports day"
    assert item["contested"] is True
    assert item["amendment_id"]


async def test_an_obligation_a_later_notice_contests_is_flagged(family, database):
    r = await family.upload("amma", pdf("g"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    obligation_id = await _obligation(database, family, artifact_id=artifact_id, due=TODAY, title="Pay the fee")
    claim_id = await database.fetchval("SELECT claim_id FROM obligations WHERE id = $1", obligation_id)
    other = (await family.upload("amma", pdf("g2"), visibility="shared")).json()["artifact"]["id"]
    new = await _claim(database, family, other, title="Fee revised")
    await database.execute(
        "INSERT INTO amendments (id, household_id, artifact_id, claim_id, amends_artifact_id, amends_claim_id, "
        "score, matched_on) VALUES ($1, $2, $3, $4, $5, $6, 0.9, 'title')",
        uuid.uuid4(), uuid.UUID(family.household["id"]), uuid.UUID(other), new, uuid.UUID(artifact_id), claim_id)

    item = next(i for i in (await _brief(family, limit=-1))["items"] if i["reason"] == "today")
    # The date shown might not be the date that holds.
    assert item["contested"] is True


async def test_quarantine_is_a_guardians_line_only(family, database):
    """Only a guardian can vouch for a quarantined item, so only a guardian is
    told it is waiting. Telling Paati would be asking her to do something the
    API will refuse."""
    r = await family.upload("amma", pdf("h"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    await database.execute("UPDATE input_artifacts SET status = 'quarantined' WHERE id = $1", uuid.UUID(artifact_id))

    assert [i["reason"] for i in (await _brief(family, "amma", limit=-1))["items"]] == ["quarantine"]
    assert (await _brief(family, "paati", limit=-1))["items"] == []


async def test_the_brief_can_be_asked_for_another_day(family, database):
    """`on=` exists so a sweep can compose tomorrow's brief tonight, and so
    these tests do not depend on what today happens to be."""
    r = await family.upload("amma", pdf("i"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    await _obligation(database, family, artifact_id=artifact_id, due=TODAY + dt.timedelta(days=1),
                      title="Tomorrow's problem")

    today = await _brief(family, limit=-1)
    assert today["items"][0]["reason"] == "soon" and today["items"][0]["when"] == "tomorrow"
    ahead = await _brief(family, limit=-1, on=(TODAY + dt.timedelta(days=1)).isoformat())
    assert ahead["items"][0]["reason"] == "today" and ahead["items"][0]["when"] == "today"
