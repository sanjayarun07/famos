async def test_household_starts_with_one_guardian_and_an_inbound_address(client):
    r = await client.post("/v1/households", json={"name": "Home", "guardian": {"display_name": "A", "email": "A@Example.com"}})
    assert r.status_code == 201
    body = r.json()
    assert body["credentials"]["member"]["role"] == "guardian"
    assert body["credentials"]["member"]["email"] == "a@example.com"
    assert body["household"]["inbound_address"].endswith("@in.familyos.test")
    me = await client.get("/v1/me", headers={"Authorization": f"Bearer {body['credentials']['token']}"})
    assert me.json()["id"] == body["credentials"]["member"]["id"]


async def test_requests_without_a_valid_token_are_refused(client):
    assert (await client.get("/v1/me")).status_code == 401
    assert (await client.get("/v1/me", headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_only_guardians_add_members_and_children_get_no_token(family):
    r = await family.client.post("/v1/household/members", headers=family.h("paati"),
                                 json={"display_name": "X", "role": "adult", "email": "x@example.com"})
    assert r.status_code == 403
    assert "token" not in family.older
    household = (await family.client.get("/v1/household", headers=family.h("paati"))).json()
    assert {m["role"] for m in household["members"]} == {"guardian", "adult", "child"}
    assert len(household["members"]) == 5


async def test_member_validation(family):
    r = await family.client.post("/v1/household/members", headers=family.h("amma"),
                                 json={"display_name": "Kid", "role": "child", "email": "kid@example.com"})
    assert r.status_code == 422
    r = await family.client.post("/v1/household/members", headers=family.h("amma"),
                                 json={"display_name": "Dup", "role": "adult", "email": "APPA@example.com"})
    assert r.status_code == 422


async def test_households_are_isolated(client, family):
    from tests.conftest import Family, pdf

    other = await Family(client).setup(name="Neighbours", guardian_email="n@example.com")
    r = await family.upload("amma", pdf("mine"), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    assert (await client.get(f"/v1/artifacts/{artifact_id}", headers=other.h("amma"))).status_code == 404
    assert (await client.get("/v1/artifacts", headers=other.h("amma"))).json() == []
