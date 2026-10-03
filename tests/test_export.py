"""Taking everything with you.

Erasure destroys what is held; this hands it over. The same scope rules have
to hold for both, or an export becomes the way around the visibility rule.
"""
import io
import json
import uuid
import zipfile

import pytest

from familyos import export
from familyos.identity import NotAllowed, NotFound, Principal
from tests.conftest import pdf


def _principal(family, who="amma"):
    member = getattr(family, who)
    return Principal(member_id=uuid.UUID(member["id"]), household_id=uuid.UUID(family.household["id"]),
                     role=member["role"], display_name=member["display_name"])


async def _claim(database, family, artifact_id, **kw):
    """A claim and its obligation, straight in."""
    household_id = uuid.UUID(family.household["id"])
    extraction_id = await database.fetchval(
        "SELECT id FROM extractions WHERE artifact_id = $1 AND superseded_at IS NULL", uuid.UUID(artifact_id))
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
        "page, match, subject_name, confidence) "
        "VALUES ($1, $2, $3, $4, $5, 'deadline', $6, $7, $8, 1, 'exact', $9, 0.91)",
        claim_id, extraction_id, household_id, uuid.UUID(artifact_id), ordinal,
        kw.get("title", "Return the swimming slip"), kw.get("date"),
        kw.get("quote", "Please return the slip by Friday"), kw.get("subject_name"))
    await database.execute(
        "INSERT INTO obligations (id, household_id, artifact_id, claim_id, kind, action, title, due_date, status) "
        "VALUES ($1, $2, $3, $4, 'task', 'sign', $5, $6, 'proposed')",
        uuid.uuid4(), household_id, uuid.UUID(artifact_id), claim_id,
        kw.get("title", "Return the swimming slip"), kw.get("date"))
    return claim_id


def _open(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


# ----------------------------------------------------------------------------
# what is in it
# ----------------------------------------------------------------------------

async def test_an_export_holds_the_original_a_readable_page_and_the_json(family, database):
    artifact_id = (await family.upload("amma", pdf("x"), filename="circular.pdf",
                                       visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id)

    data, filename, counts = await export.for_member(_principal(family))
    zf = _open(data)
    names = zf.namelist()

    assert filename.endswith(".zip") and "the-arun-family" in filename
    assert "README.md" in names and "family.md" in names
    # The originals, exactly as they arrived.
    original = next(n for n in names if n.endswith("/original.pdf"))
    assert zf.read(original) == pdf("x")
    # A page a person can read, with the words the claim came from on it.
    page = zf.read(next(n for n in names if n.endswith("/notice.md"))).decode()
    assert "Return the swimming slip" in page
    assert '"Please return the slip by Friday"' in page
    assert "Confidence 0.91" in page
    # And the structured copy the law asks for.
    assert json.loads(zf.read("data/claims.json"))[0]["quote"] == "Please return the slip by Friday"
    assert json.loads(zf.read("data/household.json"))["household"]["name"] == "The Arun family"
    assert counts == {"notices": 1, "claims": 1, "obligations": 1, "reminders": 0,
                      "events": counts["events"], "originals": 1}


async def test_the_readable_page_says_when_a_claim_was_not_grounded(family, database):
    """A fact that could not be matched to any words in the notice is the one
    a family most needs flagged, so the page says so rather than printing a
    confidence and leaving it at that."""
    artifact_id = (await family.upload("amma", pdf("y"), visibility="shared")).json()["artifact"]["id"]
    claim_id = await _claim(database, family, artifact_id)
    await database.execute("UPDATE claims SET match = NULL WHERE id = $1", claim_id)

    data, _, _ = await export.for_member(_principal(family))
    zf = _open(data)
    page = zf.read(next(n for n in zf.namelist() if n.endswith("/notice.md"))).decode()
    assert "not matched to any words in the notice" in page


async def test_a_second_hand_notice_says_so(family, database):
    artifact_id = (await family.upload("amma", pdf("z"), visibility="shared")).json()["artifact"]["id"]
    await database.execute("UPDATE input_artifacts SET source = '{\"forwarded\": true}'::jsonb WHERE id = $1",
                           uuid.UUID(artifact_id))
    await _claim(database, family, artifact_id)

    data, _, _ = await export.for_member(_principal(family))
    zf = _open(data)
    page = zf.read(next(n for n in zf.namelist() if n.endswith("/notice.md"))).decode()
    assert "second-hand" in page


async def test_the_superseded_reading_is_not_what_is_exported(family, database):
    """One extraction is current. An export shows what FamilyOS holds now, not
    every reading it ever made."""
    artifact_id = (await family.upload("amma", pdf("s"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id, title="The old reading")
    await database.execute("UPDATE extractions SET superseded_at = NOW() WHERE artifact_id = $1",
                           uuid.UUID(artifact_id))
    await _claim(database, family, artifact_id, title="The current reading")

    data, _, counts = await export.for_member(_principal(family))
    zf = _open(data)
    titles = [c["title"] for c in json.loads(zf.read("data/claims.json"))]
    assert titles == ["The current reading"]
    assert counts["claims"] == 1


# ----------------------------------------------------------------------------
# scope
# ----------------------------------------------------------------------------

async def test_an_export_is_not_a_way_round_the_visibility_rule(family, database):
    """The thing that would make this dangerous. A private notice leaves in
    its sender's export and in nobody else's -- not even a guardian's."""
    private_id = (await family.upload("amma", pdf("priv"), visibility="private")).json()["artifact"]["id"]
    shared_id = (await family.upload("appa", pdf("shar"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, private_id, title="Amma's own business")
    await _claim(database, family, shared_id, title="Everyone's business")

    mine, _, _ = await export.for_member(_principal(family, "amma"))
    theirs, _, _ = await export.for_member(_principal(family, "appa"))

    def titles(data):
        return {c["title"] for c in json.loads(_open(data).read("data/claims.json"))}

    def originals(data):
        zf = _open(data)
        return {zf.read(n) for n in zf.namelist() if "/original" in n}

    assert titles(mine) == {"Amma's own business", "Everyone's business"}
    # Appa is a guardian, and still does not get it.
    assert titles(theirs) == {"Everyone's business"}
    assert pdf("priv") in originals(mine)
    assert pdf("priv") not in originals(theirs)


async def test_a_guardian_exports_what_is_held_about_a_child(family, database):
    """The counterpart to subject erasure: the same scope, handed over instead
    of destroyed, so a guardian can see what erasing would take."""
    await family.consent(family.older)
    artifact_id = (await family.upload("amma", pdf("kid"), visibility="shared",
                                       subjects=[family.older])).json()["artifact"]["id"]
    await _claim(database, family, artifact_id, title="Older one's trip", subject_name="Older one")
    other_id = (await family.upload("amma", pdf("oth"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, other_id, title="Nothing to do with the child")

    data, filename, counts = await export.for_subject(_principal(family), uuid.UUID(family.older["id"]))
    zf = _open(data)
    assert "older-one" in filename
    assert "Everything FamilyOS holds about Older one" in zf.read("README.md").decode()
    titles = [c["title"] for c in json.loads(zf.read("data/claims.json"))]
    assert titles == ["Older one's trip"]
    # The consent that made recording anything about them lawful travels with it.
    assert json.loads(zf.read("data/household.json"))["consents"][0]["purpose"]
    assert counts["notices"] == 1


async def test_only_a_guardian_may_export_a_child(family, database):
    with pytest.raises(NotAllowed):
        await export.for_subject(_principal(family, "paati"), uuid.UUID(family.older["id"]))


async def test_an_adult_is_not_exported_as_a_subject(family, database):
    """An adult asks for their own; nobody asks on their behalf."""
    with pytest.raises(NotAllowed):
        await export.for_subject(_principal(family), uuid.UUID(family.paati["id"]))
    with pytest.raises(NotFound):
        await export.for_subject(_principal(family), uuid.uuid4())


async def test_an_adult_who_is_not_a_guardian_gets_their_own_actions_not_everyones(family, database):
    """The audit trail is the household's record for a guardian, and the
    member's own for anyone else."""
    await family.upload("amma", pdf("a1"), visibility="shared")
    await family.upload("paati", pdf("a2"), visibility="shared")

    theirs = json.loads(_open((await export.for_member(_principal(family, "paati")))[0]).read("data/audit.json"))
    assert theirs and {e["actor_member_id"] for e in theirs} == {family.paati["id"]}

    # A guardian keeps the household's record, so they see both.
    mine = json.loads(_open((await export.for_member(_principal(family, "amma")))[0]).read("data/audit.json"))
    assert {family.amma["id"], family.paati["id"]} <= {e["actor_member_id"] for e in mine}


# ----------------------------------------------------------------------------
# how it behaves
# ----------------------------------------------------------------------------

async def test_the_export_is_not_stored_anywhere(family, database):
    """A saved export would be a second, unsealed copy of the household that
    erasure would have to chase. So it is built for the request and gone."""
    before = await database.fetchval("SELECT count(*) FROM blobs")
    artifact_id = (await family.upload("amma", pdf("n"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id)
    await export.for_member(_principal(family))
    assert await database.fetchval("SELECT count(*) FROM blobs") == before + 1  # the notice, not the export
    assert await database.fetchval(
        "SELECT count(*) FROM input_artifacts WHERE media_type = 'application/zip'") == 0


async def test_asking_for_it_is_recorded_as_one_act(family, database):
    """Fifty originals read to answer one request is one deliberate act, and
    the record should say so once."""
    for tag in ("e1", "e2", "e3"):
        await family.upload("amma", pdf(tag), visibility="shared")
    await export.for_member(_principal(family))
    assert await database.fetchval("SELECT count(*) FROM audit_events WHERE action = 'export.created'") == 1
    reads = await database.fetch("SELECT detail FROM audit_events WHERE action = 'artifact.originals_read'")
    assert len(reads) == 1 and reads[0]["detail"]["artifacts"] == 3
    detail = await database.fetchval("SELECT detail FROM audit_events WHERE action = 'export.created'")
    assert detail["scope"] == "member" and detail["notices"] == 3 and detail["bytes"] > 0


async def test_an_export_bigger_than_the_ceiling_is_refused_before_it_is_built(family, database, monkeypatch):
    from familyos.artifacts import TooLarge
    from familyos.settings import settings
    monkeypatch.setattr(settings, "max_export_bytes", 10)
    await family.upload("amma", pdf("big"), visibility="shared")
    with pytest.raises(TooLarge):
        await export.for_member(_principal(family))


async def test_the_endpoint_returns_a_zip_nothing_may_cache(family, database):
    artifact_id = (await family.upload("amma", pdf("http"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id)
    r = await family.client.get("/v1/export", headers=family.h("amma"))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    assert "README.md" in _open(r.content).namelist()


async def test_an_empty_household_still_exports(family, database):
    """Nothing has arrived yet. The export says so rather than failing."""
    data, _, counts = await export.for_member(_principal(family))
    zf = _open(data)
    assert counts["notices"] == 0
    assert "The Arun family" in zf.read("family.md").decode()
    assert "Nothing has been sent yet" in zf.read("reminders.md").decode()


async def test_a_childs_export_reaches_as_far_as_their_erasure_would(family, database):
    """Erasure clears a claim naming "Older" when the child is "Older one",
    because consent.names_match accepts a shortened name. Export used string
    equality, so it handed over less than would be destroyed -- the wrong
    direction to be wrong in."""
    from familyos import consent
    assert consent.names_match("Older", "Older one")

    await family.consent(family.older)
    artifact_id = (await family.upload("amma", pdf("short"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, artifact_id, title="The shortened name", subject_name="Older")
    other = (await family.upload("amma", pdf("else"), visibility="shared")).json()["artifact"]["id"]
    await _claim(database, family, other, title="Somebody else entirely", subject_name="Appa")

    data, _, counts = await export.for_subject(_principal(family), uuid.UUID(family.older["id"]))
    titles = [c["title"] for c in json.loads(_open(data).read("data/claims.json"))]
    assert titles == ["The shortened name"]
    assert counts["notices"] == 1
