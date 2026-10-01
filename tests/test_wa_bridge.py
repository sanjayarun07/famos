"""The development bridge for unofficial WhatsApp clients.

It exists so a real class group can be read while developing, which the Cloud
API cannot do. It is off by default and these tests pin that shut.
"""
import base64
import uuid

import pytest

from familyos.db import pool
from familyos.intake import wa_bridge
from familyos.settings import settings
from tests.conftest import pdf

SECRET = "bridge-dev-secret"


def relay(*, linked="919876543210", author="919999999999", text="Sports day moved to 14 November 2026",
          is_group=True, message_id="3EB0ABC", media=None, chat="Class II Parents"):
    body = {"linked_phone": linked, "id": message_id, "timestamp": 1790000000,
            "is_group": is_group, "chat_name": chat, "author": author, "author_name": "Priya",
            "text": text}
    if media:
        body["media"] = media
    return body


@pytest.fixture(autouse=True)
def bridge_on(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_bridge_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_bridge_secret", SECRET)


async def _link(family, who="amma", number="919876543210"):
    await pool().execute("UPDATE members SET phone = $2 WHERE id = $1",
                         uuid.UUID(getattr(family, who)["id"]), number)


async def post(client, body, secret=SECRET):
    return await client.post("/v1/inbound/whatsapp/bridge", json=body,
                             headers={"X-FamilyOS-Bridge-Secret": secret})


# ----------------------------------------------------------------------------
# it stays shut unless asked for
# ----------------------------------------------------------------------------

async def test_the_bridge_is_invisible_when_disabled(family, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_bridge_enabled", False)
    r = await post(family.client, relay())
    assert r.status_code == 404        # not 401: it should not even admit to existing


async def test_a_wrong_secret_is_refused(family):
    await _link(family)
    assert (await post(family.client, relay(), secret="nope")).status_code == 401


async def test_no_secret_configured_accepts_nothing(family, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_bridge_secret", "")
    assert (await post(family.client, relay(), secret="")).status_code == 401


# ----------------------------------------------------------------------------
# a group message belongs to whoever received it
# ----------------------------------------------------------------------------

async def test_a_group_message_is_stored_for_the_linked_member(family, database):
    """The author is another parent and a stranger to this household. Treating
    them as the sender would refuse nearly every group message."""
    await _link(family, "amma")
    r = await post(family.client, relay())
    assert r.status_code == 202 and r.json()["received"] == 1

    row = dict(await database.fetchrow("SELECT * FROM input_artifacts WHERE channel = 'whatsapp'"))
    assert row["submitted_by"] == uuid.UUID(family.amma["id"])
    assert row["source"]["author"] == "919999999999"       # who typed it
    assert row["source"]["chat_name"] == "Class II Parents"
    assert row["source"]["is_group"] is True
    assert row["source"]["relay"] == "bridge"


async def test_anything_from_a_group_counts_as_second_hand(family, database):
    await _link(family)
    await post(family.client, relay(is_group=True))
    assert await database.fetchval(
        "SELECT (source->>'forwarded')::boolean FROM input_artifacts WHERE channel = 'whatsapp'") is True


async def test_an_unlinked_number_places_nothing(family, database):
    r = await post(family.client, relay(linked="910000000000"))
    assert r.json() == {"received": 0, "refused": 1, "reason": "unknown_sender"}
    assert await database.fetchval("SELECT count(*) FROM input_artifacts") == 0


async def test_a_relayed_attachment_needs_no_network(family, database):
    """The bytes ride along in the payload, so nothing calls out to Meta."""
    await _link(family, "appa")
    body = relay(linked="919876543210", text="Circular attached", media={
        "mimetype": "application/pdf", "filename": "circular.pdf",
        "data_base64": base64.b64encode(pdf("relayed")).decode()})
    await _link(family, "appa")
    r = await post(family.client, body)
    assert r.status_code == 202 and r.json()["received"] == 2

    child = dict(await database.fetchrow("SELECT * FROM input_artifacts WHERE channel = 'whatsapp_media'"))
    assert child["filename"] == "circular.pdf" and child["media_type"] == "application/pdf"


async def test_replaying_the_same_relay_stores_nothing_new(family, database):
    await _link(family)
    await post(family.client, relay())
    await post(family.client, relay())
    assert await database.fetchval("SELECT count(*) FROM input_artifacts") == 1


# ----------------------------------------------------------------------------
# the payload contract
# ----------------------------------------------------------------------------

async def test_a_payload_without_a_linked_number_is_refused(family):
    body = relay()
    body.pop("linked_phone")
    r = await post(family.client, body)
    assert r.status_code == 422 and "linked_phone" in r.json()["detail"]


async def test_a_payload_without_an_id_is_refused(family):
    body = relay()
    body["id"] = ""
    assert (await post(family.client, body)).status_code == 422


def test_bad_base64_is_refused_rather_than_stored():
    with pytest.raises(wa_bridge.BadBridgePayload):
        wa_bridge.parse(relay(media={"mimetype": "application/pdf", "data_base64": "not base64!!"}))


def test_a_direct_message_is_not_marked_second_hand():
    message, source = wa_bridge.parse(relay(is_group=False, chat=None))
    assert message.forwarded is False and source["is_group"] is False
    assert message.sender == "919876543210"
