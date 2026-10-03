"""What FamilyOS did not see.

Everything else counts what arrived. This counts what didn't, because a notice
that never arrived leaves no row behind -- and the column saying where it
lived is what decides whether to build a browser rail, a mobile rail, or a
camera.
"""
import datetime as dt
import uuid

import pytest

from familyos import gaps
from familyos.identity import Invalid, NotFound, Principal
from tests.conftest import pdf


def _principal(family, who="amma"):
    member = getattr(family, who)
    return Principal(member_id=uuid.UUID(member["id"]), household_id=uuid.UUID(family.household["id"]),
                     role=member["role"], display_name=member["display_name"])


async def _report(family, who="amma", **body):
    payload = {"title": "The swimming letter", "lived_where": "school_portal", **body}
    r = await family.client.post("/v1/intake-gaps", headers=family.h(who), json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def _summary(family, who="amma", **params):
    r = await family.client.get("/v1/intake-gaps/summary", headers=family.h(who), params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ----------------------------------------------------------------------------
# reporting one
# ----------------------------------------------------------------------------

async def test_a_member_can_say_a_notice_never_arrived(family):
    gap = await _report(family, lived_where="school_portal", had_date=True,
                        noticed_on=dt.date.today().isoformat(), note="Another parent mentioned it")
    assert gap["lived_where"] == "school_portal"
    assert gap["had_date"] is True
    assert gap["arrived_as"] is None


async def test_a_gap_needs_somewhere_it_lived(family):
    r = await family.client.post("/v1/intake-gaps", headers=family.h("amma"),
                                 json={"title": "A thing", "lived_where": "the fridge"})
    assert r.status_code == 422
    with pytest.raises(Invalid):
        await gaps.report(_principal(family), title="A thing", lived_where="the fridge")
    with pytest.raises(Invalid):
        await gaps.report(_principal(family), title="   ", lived_where="paper")


async def test_a_gap_makes_no_claims_and_no_obligations(family, database):
    """The line this must not cross. A sentence somebody typed from memory
    standing where a quote from a circular should be would be a second source
    of truth about what the school said, with nothing behind it."""
    await _report(family, title="Sports day moved to the 14th", had_date=True)
    assert await database.fetchval("SELECT count(*) FROM claims") == 0
    assert await database.fetchval("SELECT count(*) FROM obligations") == 0
    assert await database.fetchval("SELECT count(*) FROM extractions") == 0
    assert await database.fetchval("SELECT count(*) FROM input_artifacts") == 0


async def test_the_audit_trail_counts_it_without_repeating_it(family, database):
    """The family's words about a notice stay out of the audit trail, the same
    way a filename does."""
    await _report(family, title="The swimming letter for Younger one")
    detail = await database.fetchval(
        "SELECT detail FROM audit_events WHERE action = 'intake.gap_reported'")
    assert detail["lived_where"] == "school_portal"
    assert "swimming" not in str(detail) and "Younger" not in str(detail)


# ----------------------------------------------------------------------------
# who sees them
# ----------------------------------------------------------------------------

async def test_a_guardian_sees_the_households_reports_and_others_see_their_own(family):
    """Working out what intake is missing is a guardian's job, so they get the
    whole picture -- the same rule as the audit trail."""
    await _report(family, who="amma", title="Amma noticed this")
    await _report(family, who="paati", title="Paati noticed this")

    mine = (await family.client.get("/v1/intake-gaps", headers=family.h("amma"))).json()
    theirs = (await family.client.get("/v1/intake-gaps", headers=family.h("paati"))).json()
    assert {g["title"] for g in mine} == {"Amma noticed this", "Paati noticed this"}
    assert {g["title"] for g in theirs} == {"Paati noticed this"}


# ----------------------------------------------------------------------------
# the number that decides something
# ----------------------------------------------------------------------------

async def test_the_summary_says_where_the_misses_lived_and_what_that_means(family):
    await _report(family, lived_where="school_portal", had_date=True)
    await _report(family, lived_where="school_portal")
    await _report(family, lived_where="paper")
    await _report(family, lived_where="email", also_emailed=True)

    body = await _summary(family)
    where = {w["lived_where"]: w for w in body["missed_by_where"]}
    assert where["school_portal"]["count"] == 2
    assert where["school_portal"]["with_a_date"] == 1
    assert where["email"]["also_emailed"] == 1
    # The breakdown is ordered by how much it happened, worst first.
    assert body["missed_by_where"][0]["lived_where"] == "school_portal"
    # And every count carries what it would mean doing.
    assert "browser rail" in where["school_portal"]["means"]
    assert "camera" in where["paper"]["means"]


async def test_capture_rate_counts_what_arrived_against_what_was_missed(family):
    for tag in ("c1", "c2", "c3"):
        await family.upload("amma", pdf(tag), visibility="shared")
    await _report(family, lived_where="school_portal")

    body = await _summary(family)
    assert body["captured"] == 3 and body["missed_reported"] == 1
    assert body["capture_rate"] == 0.75
    assert body["captured_by_channel"] == {"upload": 3}
    # Said out loud, because the endpoint cannot caveat itself.
    assert "lower bound" in body["caveat"]


async def test_an_empty_household_has_captured_nothing_not_everything(family):
    """1.0 would be the wrong answer and the kind that gets reported upward."""
    body = await _summary(family)
    assert body["captured"] == 0 and body["missed_reported"] == 0
    assert body["capture_rate"] is None


async def test_attachments_are_not_counted_as_separate_notices(family, database):
    """One email with a circular attached is one notice that arrived, not two,
    or capture looks better the more attachments a school sends."""
    parent = (await family.upload("amma", pdf("p"), visibility="shared")).json()["artifact"]["id"]
    await database.execute(
        "INSERT INTO input_artifacts (id, household_id, blob_id, channel, visibility, status, submitted_by, "
        "media_type, size_bytes, sha256, parent_id) SELECT $1, household_id, blob_id, 'email_attachment', "
        "visibility, status, submitted_by, media_type, size_bytes, sha256, $2 FROM input_artifacts WHERE id = $2",
        uuid.uuid4(), uuid.UUID(parent))
    assert (await _summary(family))["captured"] == 1


async def test_the_window_can_be_moved(family):
    await _report(family, lived_where="paper")
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    assert (await _summary(family, since=tomorrow))["missed_reported"] == 0
    assert (await _summary(family))["missed_reported"] == 1


# ----------------------------------------------------------------------------
# closing one
# ----------------------------------------------------------------------------

async def test_a_gap_can_be_pointed_at_the_notice_that_turned_up(family):
    gap = await _report(family)
    artifact_id = (await family.upload("amma", pdf("late"), visibility="shared")).json()["artifact"]["id"]
    r = await family.client.post(f"/v1/intake-gaps/{gap['id']}/arrived", headers=family.h("amma"),
                                 json={"arrived_as": artifact_id})
    assert r.status_code == 200, r.text
    assert r.json()["arrived_as"] == artifact_id
    assert (await _summary(family))["missed_by_where"][0]["arrived_later"] == 1

    # And unset again.
    r = await family.client.post(f"/v1/intake-gaps/{gap['id']}/arrived", headers=family.h("amma"),
                                 json={"arrived_as": None})
    assert r.json()["arrived_as"] is None


async def test_resolving_is_not_a_way_to_learn_a_private_notice_exists(family):
    """Pointing a gap at somebody else's private artifact would confirm that
    artifact's id is real, which is a read the visibility rule refuses."""
    private_id = (await family.upload("amma", pdf("secret"), visibility="private")).json()["artifact"]["id"]
    gap = await _report(family, who="appa")
    with pytest.raises(NotFound):
        await gaps.resolve(_principal(family, "appa"), uuid.UUID(gap["id"]), uuid.UUID(private_id))
    with pytest.raises(NotFound):
        await gaps.resolve(_principal(family, "appa"), uuid.uuid4(), None)
