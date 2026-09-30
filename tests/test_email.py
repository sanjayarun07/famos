from email.message import EmailMessage

from tests.conftest import pdf

PROVIDER_PASS = "mx.provider.test; dkim=pass header.d=example.com; spf=pass smtp.mailfrom=example.com; dmarc=pass header.from=example.com"


def message(sender: str, to: str, *, auth: str | None = PROVIDER_PASS, message_id: str = "<m1@example.com>",
            attachments=(("trip.pdf", pdf("trip")),), forged_auth: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = f"Someone <{sender}>"
    msg["To"] = to
    msg["Subject"] = "Fwd: Annual trip, revised"
    msg["Message-ID"] = message_id
    if auth:
        msg["Authentication-Results"] = auth        # added by the receiving provider: topmost
    if forged_auth:
        msg["Authentication-Results"] = forged_auth
    msg.set_content("Please see the revised notice attached.")
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


async def post(client, raw: bytes, secret: str = "test-secret", recipient: str | None = None):
    headers = {"X-FamilyOS-Webhook-Secret": secret, "Content-Type": "message/rfc822"}
    if recipient:
        headers["X-FamilyOS-Recipient"] = recipient
    return await client.post("/v1/inbound/email", content=raw, headers=headers)


async def test_forward_from_a_member_is_accepted_with_its_attachment(family):
    r = await post(family.client, message("appa@example.com", family.inbound))
    assert r.status_code == 202 and r.json()["status"] == "accepted", r.text
    listed = (await family.client.get("/v1/artifacts", headers=family.h("appa"))).json()
    assert len(listed) == 1
    email = listed[0]
    assert email["channel"] == "email" and email["visibility"] == "private"
    assert email["submitted_by"] == family.appa["id"]
    assert email["source"]["subject"] == "Fwd: Annual trip, revised"
    assert len(email["children"]) == 1
    child = (await family.client.get(f"/v1/artifacts/{email['children'][0]}", headers=family.h("appa"))).json()
    assert child["channel"] == "email_attachment" and child["filename"] == "trip.pdf"
    original = await family.client.get(f"/v1/artifacts/{child['id']}/original", headers=family.h("appa"))
    assert original.content == pdf("trip")
    # Private to the member who forwarded it.
    assert (await family.client.get("/v1/artifacts", headers=family.h("amma"))).json() == []


async def test_the_same_message_delivered_twice_is_stored_once(family, database):
    raw = message("appa@example.com", family.inbound)
    first, second = await post(family.client, raw), await post(family.client, raw)
    assert second.json()["duplicate"] is True and second.json()["id"] == first.json()["id"]
    assert await database.fetchval("SELECT count(*) FROM input_artifacts") == 2       # the email and its PDF


async def test_unknown_sender_is_quarantined_until_a_guardian_vouches(family):
    r = await post(family.client, message("school-office@school.test", family.inbound,
                                          auth="mx.provider.test; dmarc=pass header.from=school.test"))
    assert r.json()["status"] == "quarantined"
    assert (await family.client.get("/v1/artifacts", headers=family.h("amma"))).json() == []
    assert (await family.client.get("/v1/quarantine", headers=family.h("paati"))).status_code == 403
    queue = (await family.client.get("/v1/quarantine", headers=family.h("amma"))).json()
    assert len(queue) == 1 and queue[0]["quarantine_reason"] == "unknown_sender"
    accepted = await family.client.post(f"/v1/quarantine/{queue[0]['id']}/accept", headers=family.h("amma"),
                                        json={"member_id": family.appa["id"], "visibility": "shared"})
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    listed = (await family.client.get("/v1/artifacts", headers=family.h("paati"))).json()
    assert [a["id"] for a in listed] == [queue[0]["id"]]
    child = await family.client.get(f"/v1/artifacts/{listed[0]['children'][0]}", headers=family.h("paati"))
    assert child.status_code == 200


async def test_a_spoofed_member_address_is_quarantined(family):
    r = await post(family.client, message("appa@example.com", family.inbound, auth="mx.provider.test; dkim=fail; dmarc=fail",
                                          forged_auth="evil; dmarc=pass header.from=example.com"))
    assert r.json()["status"] == "quarantined"
    queue = (await family.client.get("/v1/quarantine", headers=family.h("amma"))).json()
    assert queue[0]["quarantine_reason"] == "sender_not_authenticated"


async def test_rejecting_quarantine_deletes_it(family, database):
    r = await post(family.client, message("stranger@spam.test", family.inbound))
    assert (await family.client.post(f"/v1/quarantine/{r.json()['id']}/reject", headers=family.h("amma"))).status_code == 204
    assert await database.fetchval("SELECT count(*) FROM input_artifacts") == 0
    assert await database.fetchval("SELECT count(*) FROM blobs") == 0


async def test_unknown_address_and_bad_secret_are_refused(family, database):
    assert (await post(family.client, message("appa@example.com", "nobody@in.familyos.test"))).status_code == 404
    assert (await post(family.client, message("appa@example.com", family.inbound), secret="wrong")).status_code == 401
    assert await database.fetchval("SELECT count(*) FROM input_artifacts") == 0


async def test_envelope_recipient_and_plus_tags_route_to_the_household(family):
    local, domain = family.inbound.split("@")
    r = await post(family.client, message("amma@example.com", "list@lists.school.test", message_id="<m2@x>"),
                   recipient=f"{local}+school@{domain}")
    assert r.status_code == 202 and r.json()["status"] == "accepted"


async def test_a_forged_verdict_is_not_believed_when_the_provider_added_none(family):
    """Anyone can write an Authentication-Results header. Without one from
    our own provider, a sender's own verdict must not authenticate them."""
    r = await post(family.client, message("appa@example.com", family.inbound,
                                          auth="evil.attacker.test; dmarc=pass header.from=example.com"))
    assert r.json()["status"] == "quarantined"
    queue = (await family.client.get("/v1/quarantine", headers=family.h("amma"))).json()
    assert queue[0]["quarantine_reason"] == "sender_not_authenticated"


async def test_mail_with_no_verdict_at_all_is_quarantined(family):
    r = await post(family.client, message("appa@example.com", family.inbound, auth=None))
    assert r.json()["status"] == "quarantined"


async def test_a_parent_domains_signature_does_not_authenticate_a_subdomain(family):
    """dkim=pass for example.com says nothing about mail.example.com; that
    is relaxed DMARC alignment, and DMARC did not pass here."""
    r = await family.client.post("/v1/household/members", headers=family.h("amma"),
                                 json={"display_name": "Thatha", "role": "adult", "email": "t@mail.example.com"})
    assert r.status_code == 201, r.text
    r = await post(family.client, message("t@mail.example.com", family.inbound,
                                          auth="mx.provider.test; dkim=pass header.d=example.com"))
    assert r.json()["status"] == "quarantined"
    # The same signature does authenticate the domain it actually names.
    r = await post(family.client, message("appa@example.com", family.inbound, message_id="<m9@x>",
                                          auth="mx.provider.test; dkim=pass header.d=example.com"))
    assert r.json()["status"] == "accepted"


async def test_only_the_configured_provider_is_believed(family, monkeypatch):
    from familyos.settings import settings
    monkeypatch.setattr(settings, "inbound_authserv_id", "")
    r = await post(family.client, message("appa@example.com", family.inbound))
    assert r.json()["status"] == "quarantined", "unset authserv-id must fail closed"
