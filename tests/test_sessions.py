"""Tokens that end: expiry, sliding renewal, sign-out and revocation."""
import uuid

from familyos import identity
from familyos.settings import settings


async def test_a_token_is_issued_with_an_end(family, database):
    row = await database.fetchrow(
        "SELECT expires_at, revoked_at FROM member_tokens t JOIN members m ON m.id = t.member_id "
        "WHERE m.display_name = 'Amma'")
    assert row["expires_at"] is not None and row["revoked_at"] is None


async def test_an_expired_token_stops_working(family, database):
    assert (await family.client.get("/v1/me", headers=family.h("paati"))).status_code == 200
    await database.execute(
        "UPDATE member_tokens SET expires_at = NOW() - INTERVAL '1 second' WHERE member_id = $1",
        uuid.UUID(family.paati["id"]))
    assert (await family.client.get("/v1/me", headers=family.h("paati"))).status_code == 401


async def test_using_a_token_slides_its_end_forward(family, database):
    # An hour of no use, and an expiry closer than the full lifetime.
    await database.execute(
        "UPDATE member_tokens SET last_used_at = NOW() - INTERVAL '2 hours', "
        "expires_at = NOW() + INTERVAL '2 days' WHERE member_id = $1", uuid.UUID(family.appa["id"]))
    before = await database.fetchval("SELECT expires_at FROM member_tokens WHERE member_id = $1",
                                     uuid.UUID(family.appa["id"]))
    assert (await family.client.get("/v1/me", headers=family.h("appa"))).status_code == 200
    after = await database.fetchval("SELECT expires_at FROM member_tokens WHERE member_id = $1",
                                    uuid.UUID(family.appa["id"]))
    assert after > before
    assert (after - before).days >= settings.token_lifetime_days - 3


async def test_a_busy_session_is_not_a_write_per_request(family, database):
    """The slide is throttled to once an hour, so reading twice in a row does
    not rewrite the row twice."""
    await family.client.get("/v1/me", headers=family.h("appa"))
    first = await database.fetchval("SELECT last_used_at FROM member_tokens WHERE member_id = $1",
                                    uuid.UUID(family.appa["id"]))
    await family.client.get("/v1/me", headers=family.h("appa"))
    second = await database.fetchval("SELECT last_used_at FROM member_tokens WHERE member_id = $1",
                                     uuid.UUID(family.appa["id"]))
    assert first == second


async def test_signing_out_ends_that_token_and_leaves_other_devices_alone(family, database):
    """The whole point. Before this, sign-out only forgot the token in the
    browser and it kept working for anyone who had it."""
    phone = (await family.client.post("/v1/household/members", headers=family.h("amma"),
                                      json={"display_name": "Chithi", "role": "adult",
                                            "email": "chithi@example.com"})).json()
    laptop_token = phone["token"]
    # A second token for the same member: another device.
    async with __import__("familyos").db.pool().acquire() as conn:
        second = await identity._issue_token(conn, uuid.UUID(phone["member"]["id"]))

    h = {"Authorization": "Bearer " + laptop_token}
    assert (await family.client.get("/v1/me", headers=h)).status_code == 200

    out = await family.client.post("/v1/signout", headers=h)
    assert out.status_code == 200 and out.json()["ended"] == 1

    assert (await family.client.get("/v1/me", headers=h)).status_code == 401
    # The other device is untouched.
    assert (await family.client.get(
        "/v1/me", headers={"Authorization": "Bearer " + second})).status_code == 200
    assert await database.fetchval(
        "SELECT revoked_reason FROM member_tokens WHERE revoked_at IS NOT NULL") == "signed_out"


async def test_a_guardian_can_sign_another_member_out_everywhere(family, database):
    """The answer to a lost phone."""
    member_id = family.paati["id"]
    assert (await family.client.get("/v1/me", headers=family.h("paati"))).status_code == 200

    out = await family.client.post(f"/v1/members/{member_id}/signout", headers=family.h("amma"))
    assert out.status_code == 200 and out.json()["ended"] >= 1
    assert (await family.client.get("/v1/me", headers=family.h("paati"))).status_code == 401
    # Amma is still signed in; only Paati was ended.
    assert (await family.client.get("/v1/me", headers=family.h("amma"))).status_code == 200


async def test_an_adult_cannot_sign_someone_else_out(family):
    r = await family.client.post(f"/v1/members/{family.amma['id']}/signout", headers=family.h("paati"))
    assert r.status_code == 403
    # But may end their own.
    own = await family.client.post(f"/v1/members/{family.paati['id']}/signout", headers=family.h("paati"))
    assert own.status_code == 200


async def test_sessions_list_shows_which_one_you_are_on(family):
    rows = (await family.client.get("/v1/sessions", headers=family.h("amma"))).json()
    assert len(rows) == 1
    assert rows[0]["current"] is True and rows[0]["expires_at"] is not None
    assert "token" not in rows[0] and "token_hash" not in rows[0]

    # An adult cannot read a guardian's sessions; a guardian can read theirs.
    assert (await family.client.get(f"/v1/sessions?member_id={family.amma['id']}",
                                    headers=family.h("paati"))).status_code == 403
    seen = await family.client.get(f"/v1/sessions?member_id={family.paati['id']}", headers=family.h("amma"))
    assert seen.status_code == 200 and seen.json()[0]["current"] is False


async def test_erasing_a_household_still_ends_every_session(family):
    r = await family.client.post("/v1/household/erase", headers=family.h("amma"),
                                 json={"confirm_name": "The Arun family"})
    assert r.status_code == 202
    assert (await family.client.get("/v1/me", headers=family.h("amma"))).status_code == 401
    assert (await family.client.get("/v1/me", headers=family.h("appa"))).status_code == 401
