from pathlib import Path

from familyos.settings import settings
from tests.conftest import pdf


async def test_private_upload_is_visible_only_to_its_sender(family):
    r = await family.upload("amma", pdf("a"))
    assert r.status_code == 201, r.text
    artifact = r.json()["artifact"]
    assert artifact["visibility"] == "private" and artifact["status"] == "accepted"
    assert artifact["media_type"] == "application/pdf"
    for who, sees in (("amma", True), ("appa", False), ("paati", False)):
        listed = (await family.client.get("/v1/artifacts", headers=family.h(who))).json()
        assert (artifact["id"] in [a["id"] for a in listed]) is sees
        got = await family.client.get(f"/v1/artifacts/{artifact['id']}", headers=family.h(who))
        assert got.status_code == (200 if sees else 404)


async def test_shared_upload_is_visible_to_every_signed_in_member(family):
    artifact = (await family.upload("appa", pdf("b"), visibility="shared")).json()["artifact"]
    for who in ("amma", "appa", "paati"):
        assert (await family.client.get(f"/v1/artifacts/{artifact['id']}", headers=family.h(who))).status_code == 200


async def test_resending_the_same_bytes_returns_the_same_artifact(family, database):
    first = await family.upload("amma", pdf("c"))
    again = await family.upload("amma", pdf("c"), filename="renamed.pdf")
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["artifact"]["id"] == first.json()["artifact"]["id"]
    # Another member sending the same bytes gets their own envelope over one stored blob.
    theirs = await family.upload("appa", pdf("c"))
    assert theirs.status_code == 201 and theirs.json()["artifact"]["id"] != first.json()["artifact"]["id"]
    assert await database.fetchval("SELECT count(*) FROM blobs") == 1


async def test_original_comes_back_byte_for_byte_and_is_stored_encrypted(family, database):
    data = pdf("secret school notice")
    artifact = (await family.upload("amma", data, filename="Trip notice.pdf")).json()["artifact"]
    r = await family.client.get(f"/v1/artifacts/{artifact['id']}/original", headers=family.h("amma"))
    assert r.status_code == 200 and r.content == data
    assert "Trip%20notice.pdf" in r.headers["content-disposition"]
    key = await database.fetchval("SELECT storage_key FROM blobs")
    stored = (Path(settings.blob_dir) / key).read_bytes()
    assert b"secret school notice" not in stored and len(stored) > len(data)
    actions = [r["action"] for r in await database.fetch("SELECT action FROM audit_events ORDER BY id")]
    assert "artifact.original_read" in actions


async def test_type_comes_from_the_bytes_not_the_label(family):
    r = await family.upload("amma", b"\x89PNG\r\n\x1a\n" + b"0" * 100, filename="photo.pdf", content_type="application/pdf")
    assert r.json()["artifact"]["media_type"] == "image/png"
    r = await family.upload("amma", b"MZ\x90\x00\x03\x00\x00\x00\x04\x00", filename="notice.pdf")
    assert r.status_code == 415
    r = await family.upload("amma", b"", filename="empty.pdf")
    assert r.status_code == 422


async def test_only_the_sender_changes_visibility(family):
    artifact = (await family.upload("amma", pdf("d"), visibility="shared")).json()["artifact"]
    r = await family.client.patch(f"/v1/artifacts/{artifact['id']}", headers=family.h("appa"), json={"visibility": "private"})
    assert r.status_code == 403
    r = await family.client.patch(f"/v1/artifacts/{artifact['id']}", headers=family.h("amma"), json={"visibility": "private"})
    assert r.status_code == 200 and r.json()["visibility"] == "private"
    assert (await family.client.get(f"/v1/artifacts/{artifact['id']}", headers=family.h("appa"))).status_code == 404


async def test_deleting_an_artifact_removes_an_unshared_blob(family, database):
    artifact = (await family.upload("amma", pdf("e"))).json()["artifact"]
    key = await database.fetchval("SELECT storage_key FROM blobs")
    assert (await family.client.delete(f"/v1/artifacts/{artifact['id']}", headers=family.h("appa"))).status_code == 404
    assert (await family.client.delete(f"/v1/artifacts/{artifact['id']}", headers=family.h("amma"))).status_code == 204
    assert await database.fetchval("SELECT count(*) FROM blobs") == 0
    assert not (Path(settings.blob_dir) / key).exists()


async def test_a_pasted_message_is_a_notice_like_any_other(family, database):
    """Until a WhatsApp business number is live, copying the message and
    pasting it is the way in. The console sends it as a text file, so it takes
    the same path as every other upload: same bytes, same hash, same dedup."""
    text = ("Dear Parents\n\nAnnual Day is on Friday, 14 November 2026 at 5:00 p.m.\n"
            "Kindly confirm attendance by 7 November 2026.\n")
    r = await family.client.post("/v1/artifacts", headers=family.h("amma"),
                                 files={"file": ("pasted 2026-09-30 18:40.txt", text.encode(), "text/plain")},
                                 data={"visibility": "shared"})
    assert r.status_code == 201, r.text
    artifact = r.json()["artifact"]
    assert artifact["media_type"] == "text/plain"
    assert artifact["filename"] == "pasted 2026-09-30 18:40.txt"

    # Two parents pasting the same message store the bytes once.
    again = await family.client.post("/v1/artifacts", headers=family.h("appa"),
                                     files={"file": ("pasted again.txt", text.encode(), "text/plain")},
                                     data={"visibility": "shared"})
    assert again.status_code == 201
    assert await database.fetchval("SELECT count(*) FROM blobs") == 1
    assert await database.fetchval("SELECT count(*) FROM input_artifacts") == 2


async def test_the_household_says_which_ways_in_are_live(family, monkeypatch):
    """The console only offers a way in that actually works, so it has to be
    told whether a WhatsApp number is configured."""
    from familyos.settings import settings

    monkeypatch.setattr(settings, "whatsapp_business_number", "")
    off = (await family.client.get("/v1/household", headers=family.h("amma"))).json()
    assert off["whatsapp_number"] is None
    assert off["inbound_address"].endswith("@in.familyos.test")

    monkeypatch.setattr(settings, "whatsapp_business_number", "+91 80 4567 8910")
    on = (await family.client.get("/v1/household", headers=family.h("amma"))).json()
    assert on["whatsapp_number"] == "+91 80 4567 8910"
