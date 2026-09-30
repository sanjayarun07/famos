"""WhatsApp intake: what a parent forwards to the business number."""
import hashlib
import hmac
import json
import uuid

import pytest

from familyos.intake import whatsapp
from familyos.settings import settings
from tests.conftest import pdf

SECRET = "test-app-secret"


def delivery(*, sender="919876543210", text=None, media=None, message_id="wamid.TEST1",
             name="Amma", forwarded=True) -> dict:
    """The shape Meta actually posts."""
    message = {"from": sender, "id": message_id, "timestamp": "1790000000"}
    if forwarded:
        message["context"] = {"forwarded": True}
    if media:
        message["type"] = media["kind"]
        message[media["kind"]] = {k: v for k, v in media.items() if k != "kind"}
    else:
        message["type"] = "text"
        message["text"] = {"body": text or ""}
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "contacts": [{"wa_id": sender, "profile": {"name": name}}],
        "messages": [message]}}]}]}


def signed(payload: dict) -> tuple[bytes, dict]:
    raw = json.dumps(payload).encode()
    mac = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": "sha256=" + mac, "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def cloud_api(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_app_secret", SECRET)
    monkeypatch.setattr(settings, "whatsapp_verify_token", "verify-me")


async def _with_phone(family, who="amma", number="919876543210"):
    """Give a member a phone the way a guardian would."""
    member = getattr(family, who)
    from familyos.db import pool
    await pool().execute("UPDATE members SET phone = $2 WHERE id = $1", uuid.UUID(member["id"]), number)
    return member


# ----------------------------------------------------------------------------
# the webhook itself
# ----------------------------------------------------------------------------

async def test_an_unsigned_delivery_is_refused(family):
    raw = json.dumps(delivery(text="hello")).encode()
    r = await family.client.post("/v1/inbound/whatsapp", content=raw,
                                 headers={"Content-Type": "application/json"})
    assert r.status_code == 401


async def test_a_wrongly_signed_delivery_is_refused(family):
    raw, headers = signed(delivery(text="hello"))
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    r = await family.client.post("/v1/inbound/whatsapp", content=raw, headers=headers)
    assert r.status_code == 401


async def test_without_a_configured_secret_nothing_is_accepted(family, monkeypatch):
    """An unsigned webhook is an open door to anyone who learns the URL."""
    monkeypatch.setattr(settings, "whatsapp_app_secret", "")
    raw, headers = signed(delivery(text="hello"))
    r = await family.client.post("/v1/inbound/whatsapp", content=raw, headers=headers)
    assert r.status_code == 401


async def test_the_subscription_handshake(family):
    ok = await family.client.get("/v1/inbound/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = await family.client.get("/v1/inbound/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"})
    assert bad.status_code == 403


# ----------------------------------------------------------------------------
# placing a message in a household
# ----------------------------------------------------------------------------

async def test_a_forwarded_notice_becomes_an_artifact(family, database):
    await _with_phone(family, "amma")
    raw, headers = signed(delivery(text="Sports day is on Friday 14 November 2026 at 8:30 a.m."))
    r = await family.client.post("/v1/inbound/whatsapp", content=raw, headers=headers)
    assert r.status_code == 202 and r.json()["received"] == 1

    row = dict(await database.fetchrow("SELECT * FROM input_artifacts WHERE channel = 'whatsapp'"))
    assert row["status"] == "accepted"
    assert row["submitted_by"] == uuid.UUID(family.amma["id"])
    assert row["visibility"] == "private"
    assert row["source"]["from"] == "919876543210"
    assert row["source"]["forwarded"] is True
    assert row["media_type"] == "text/plain"


async def test_a_number_belonging_to_nobody_is_refused_and_stored_nowhere(family, database):
    """Unlike email there is no household address on a WhatsApp message, so a
    number we cannot place has no household to be quarantined into."""
    raw, headers = signed(delivery(sender="919999999999", text="hello?"))
    r = await family.client.post("/v1/inbound/whatsapp", content=raw, headers=headers)
    # Meta retries anything that is not 2xx, and this will never become
    # placeable, so it is accepted and dropped.
    assert r.status_code == 202 and r.json() == {"received": 0, "refused": 1}
    assert await database.fetchval("SELECT count(*) FROM input_artifacts") == 0


async def test_a_number_in_two_households_places_nothing(family, database, client):
    """Guessing which family a school notice belongs to is not a guess worth
    making."""
    await _with_phone(family, "amma", "919876543210")
    other = await client.post("/v1/households", json={
        "name": "Another family", "guardian": {"display_name": "Someone", "email": "x@example.com"}})
    await database.execute("UPDATE members SET phone = $2 WHERE id = $1",
                           uuid.UUID(other.json()["credentials"]["member"]["id"]), "919876543210")

    raw, headers = signed(delivery(text="whose is this?"))
    r = await family.client.post("/v1/inbound/whatsapp", content=raw, headers=headers)
    assert r.json()["refused"] == 1
    assert await database.fetchval("SELECT count(*) FROM input_artifacts") == 0


async def test_the_same_message_delivered_twice_is_stored_once(family, database):
    await _with_phone(family, "amma")
    raw, headers = signed(delivery(text="Fee due 5 December 2026"))
    first = await family.client.post("/v1/inbound/whatsapp", content=raw, headers=headers)
    second = await family.client.post("/v1/inbound/whatsapp", content=raw, headers=headers)
    assert first.json()["received"] == 1 and second.json()["received"] == 1
    assert await database.fetchval("SELECT count(*) FROM input_artifacts") == 1


async def test_an_attachment_becomes_a_child_of_the_message(family, database, monkeypatch):
    await _with_phone(family, "appa")
    from familyos.intake import gateway

    async def fake_fetch(media_id):
        assert media_id == "MEDIA-1"
        return pdf("circular"), "application/pdf"

    real = gateway.receive_whatsapp

    async def with_fetch(message, **kw):
        return await real(message, fetch=fake_fetch)

    monkeypatch.setattr(gateway, "receive_whatsapp", with_fetch)

    raw, headers = signed(delivery(
        sender="919876543210",
        media={"kind": "document", "id": "MEDIA-1", "mime_type": "application/pdf",
               "filename": "circular.pdf", "caption": "Trip circular, please sign"}))
    await family.client.post("/v1/inbound/whatsapp", content=raw, headers=headers)

    parent = dict(await database.fetchrow("SELECT * FROM input_artifacts WHERE channel = 'whatsapp'"))
    child = dict(await database.fetchrow("SELECT * FROM input_artifacts WHERE channel = 'whatsapp_media'"))
    assert child["parent_id"] == parent["id"]
    assert child["filename"] == "circular.pdf" and child["media_type"] == "application/pdf"
    # The words around a forwarded file are often where the date is.
    assert parent["size_bytes"] > 0


async def test_a_member_can_see_what_they_forwarded(family, monkeypatch):
    await _with_phone(family, "amma")
    raw, headers = signed(delivery(text="PTM on Saturday 3 October 2026"))
    await family.client.post("/v1/inbound/whatsapp", content=raw, headers=headers)

    mine = (await family.client.get("/v1/artifacts", headers=family.h("amma"))).json()
    assert len(mine) == 1 and mine[0]["channel"] == "whatsapp"
    # Private to whoever forwarded it, like every other channel.
    assert (await family.client.get("/v1/artifacts", headers=family.h("appa"))).json() == []


# ----------------------------------------------------------------------------
# the adapter
# ----------------------------------------------------------------------------

def test_status_callbacks_carry_no_messages():
    """Meta sends delivered/read receipts down the same webhook."""
    assert whatsapp.parse({"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.X"}]}}]}]}) == []


def test_reactions_and_locations_are_not_notices():
    payload = delivery(text="x")
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "from": "919876543210", "id": "wamid.R", "type": "reaction", "reaction": {"emoji": "\U0001F44D"}}
    assert whatsapp.parse(payload) == []


def test_a_number_is_matched_however_it_was_typed():
    assert whatsapp.normalise_phone("+91 98765 43210") == "919876543210"
    assert whatsapp.normalise_phone("098765-43210") == "09876543210"
    assert whatsapp.normalise_phone("") is None
    # Meta always sends the country code; a guardian may have typed the local form.
    assert "9876543210" in whatsapp.phone_variants("919876543210")


def test_the_signature_is_checked_constant_time():
    raw = b'{"a":1}'
    good = hmac.new(b"s", raw, hashlib.sha256).hexdigest()
    whatsapp.verify_signature(raw, "sha256=" + good, "s")
    with pytest.raises(whatsapp.BadSignature):
        whatsapp.verify_signature(raw, "sha256=" + "f" * 64, "s")
    with pytest.raises(whatsapp.BadSignature):
        whatsapp.verify_signature(raw, None, "s")
    with pytest.raises(whatsapp.BadSignature):
        whatsapp.verify_signature(raw, "sha256=" + good, "")
