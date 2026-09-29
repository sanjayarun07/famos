from pathlib import Path

from familyos import jobs
from familyos.settings import settings
from tests.conftest import pdf


async def test_naming_a_child_needs_consent_first(family):
    r = await family.upload("amma", pdf("a"), subjects=[family.older])
    assert r.status_code == 409 and r.json()["error"] == "consent_required"
    await family.consent(family.older)
    r = await family.upload("amma", pdf("a"), subjects=[family.older])
    assert r.status_code == 201 and r.json()["artifact"]["subject_member_ids"] == [family.older["id"]]
    # Adults need no child consent record.
    assert (await family.upload("amma", pdf("b"), subjects=[family.paati])).status_code == 201


async def test_only_guardians_consent_and_only_for_children(family):
    r = await family.client.post("/v1/consents", headers=family.h("paati"), json={
        "subject_member_id": family.older["id"], "notice_version": "1", "verification_method": "account_holder"})
    assert r.status_code == 403
    r = await family.client.post("/v1/consents", headers=family.h("amma"), json={
        "subject_member_id": family.paati["id"], "notice_version": "1", "verification_method": "account_holder"})
    assert r.status_code == 422
    await family.consent(family.older)
    r = await family.client.post("/v1/consents", headers=family.h("appa"), json={
        "subject_member_id": family.older["id"], "notice_version": "1", "verification_method": "account_holder"})
    assert r.status_code == 422      # already active


async def test_setting_subjects_later_also_needs_consent(family):
    artifact = (await family.upload("amma", pdf("c"), visibility="shared")).json()["artifact"]
    r = await family.client.put(f"/v1/artifacts/{artifact['id']}/subjects", headers=family.h("appa"), json=[family.younger["id"]])
    assert r.status_code == 409
    await family.consent(family.younger)
    r = await family.client.put(f"/v1/artifacts/{artifact['id']}/subjects", headers=family.h("appa"), json=[family.younger["id"]])
    assert r.status_code == 200 and r.json()["subject_member_ids"] == [family.younger["id"]]


async def test_withdrawing_consent_erases_what_names_the_child(family, database):
    consent = await family.consent(family.older)
    about_older = (await family.upload("amma", pdf("older"), subjects=[family.older])).json()["artifact"]
    unrelated = (await family.upload("amma", pdf("unrelated"))).json()["artifact"]
    key = await database.fetchval("SELECT storage_key FROM blobs WHERE sha256 = $1", about_older["sha256"])

    r = await family.client.post(f"/v1/consents/{consent['id']}/withdraw", headers=family.h("appa"))
    assert r.status_code == 202, r.text
    erasure = r.json()
    job_id = await database.fetchval("SELECT job_id::text FROM erasure_log WHERE id = $1", erasure["id"])
    done = await jobs.attach(job_id.replace("-", ""))
    assert done["status"] == "succeeded", done

    ids = [a["id"] for a in (await family.client.get("/v1/artifacts", headers=family.h("amma"))).json()]
    assert about_older["id"] not in ids and unrelated["id"] in ids
    assert not (Path(settings.blob_dir) / key).exists()
    status = (await family.client.get(f"/v1/erasures/{erasure['id']}", headers=family.h("amma"))).json()
    assert status["completed_at"] and status["counts"]["artifacts"] == 1 and status["counts"]["member_deleted"] is False
    consents = (await family.client.get("/v1/consents", headers=family.h("amma"))).json()
    assert consents[0]["withdrawn_at"] and consents[0]["withdrawn_by"] == family.appa["id"]
    # New records about the child are refused again.
    assert (await family.upload("amma", pdf("again"), subjects=[family.older])).status_code == 409


async def test_erasing_a_child_member_removes_the_member(family, database):
    await family.consent(family.younger)
    await family.upload("amma", pdf("y"), subjects=[family.younger])
    r = await family.client.post(f"/v1/members/{family.younger['id']}/erase", headers=family.h("amma"))
    assert r.status_code == 202
    job_id = await database.fetchval("SELECT job_id::text FROM erasure_log")
    assert (await jobs.attach(job_id.replace("-", "")))["status"] == "succeeded"
    members = (await family.client.get("/v1/household", headers=family.h("amma"))).json()["members"]
    assert family.younger["id"] not in [m["id"] for m in members]
    assert await database.fetchval("SELECT count(*) FROM consent_records") == 0
    assert (await family.client.post(f"/v1/members/{family.paati['id']}/erase", headers=family.h("amma"))).status_code == 403
