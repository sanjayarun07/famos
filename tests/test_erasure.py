from pathlib import Path

import pytest

from familyos import artifacts, blobstore, jobs
from familyos.settings import settings
from tests.conftest import Family, pdf


async def _run(database, erasure_id):
    job_id = await database.fetchval("SELECT job_id::text FROM erasure_log WHERE id = $1", erasure_id)
    return await jobs.attach(job_id)


async def test_household_erasure_needs_the_name_typed(family):
    r = await family.client.post("/v1/household/erase", headers=family.h("amma"), json={"confirm_name": "wrong"})
    assert r.status_code == 422
    r = await family.client.post("/v1/household/erase", headers=family.h("paati"), json={"confirm_name": "The Arun family"})
    assert r.status_code == 403


async def test_household_erasure_removes_everything_and_keeps_a_receipt(client, family, database):
    other = await Family(client).setup(name="Neighbours", guardian_email="n@example.com")
    await other.upload("amma", pdf("theirs"))
    await family.consent(family.older)
    await family.upload("amma", pdf("one"), subjects=[family.older])
    await family.upload("appa", pdf("two"), visibility="shared")
    keys = [r["storage_key"] for r in await database.fetch(
        "SELECT storage_key FROM blobs WHERE household_id = $1", family.household["id"])]

    r = await client.post("/v1/household/erase", headers=family.h("amma"), json={"confirm_name": "The Arun family"})
    assert r.status_code == 202, r.text
    # Sign-in and intake stop immediately, before the job runs.
    assert (await client.get("/v1/me", headers=family.h("appa"))).status_code == 401

    done = await _run(database, r.json()["id"])
    assert done["status"] == "succeeded", done
    hid = family.household["id"]
    for table in ("households", "members", "input_artifacts", "blobs", "consent_records", "audit_events"):
        column = "id" if table == "households" else "household_id"
        assert await database.fetchval(f"SELECT count(*) FROM {table} WHERE {column} = $1", hid) == 0, table
    assert not any((Path(settings.blob_dir) / k).exists() for k in keys)
    log = await database.fetchrow("SELECT * FROM erasure_log WHERE id = $1", r.json()["id"])
    assert log["completed_at"] and log["counts"] == {"blobs": 2, "artifacts": 2, "members": 5}
    # The neighbours are untouched.
    assert len((await client.get("/v1/artifacts", headers=other.h("amma"))).json()) == 1


async def test_the_key_is_destroyed_before_objects_are_deleted(family, database):
    """If object deletion fails, the job retries, and the originals are
    already unreadable because the household key is gone."""
    await family.upload("amma", pdf("x"))
    real = blobstore.store()

    class Failing(blobstore.LocalBlobStore):
        async def delete(self, key):
            raise OSError("object store down")

    blobstore.use(Failing(settings.blob_dir))
    r = await family.client.post("/v1/household/erase", headers=family.h("amma"), json={"confirm_name": "The Arun family"})
    row = await _run(database, r.json()["id"])
    assert row["status"] == "queued" and "could not be deleted" in row["error"]
    assert await database.fetchval("SELECT wrapped_key FROM households WHERE id = $1", family.household["id"]) is None

    blobstore.use(real)
    row = await _run(database, r.json()["id"])
    assert row["status"] == "succeeded"


async def test_nothing_new_is_accepted_while_erasing(family, database):
    await family.client.post("/v1/household/erase", headers=family.h("amma"), json={"confirm_name": "The Arun family"})
    async with database.acquire() as conn:
        with pytest.raises(artifacts.HouseholdUnavailable):
            await artifacts.create(conn, family.household["id"], artifacts.NewArtifact(
                data=b"x", media_type="text/plain", channel="upload", visibility="private", status="accepted"))
