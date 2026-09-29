import asyncpg
import pytest

from tests.conftest import pdf


async def test_audit_is_append_only(family, database):
    with pytest.raises(asyncpg.RaiseError):
        await database.execute("UPDATE audit_events SET action = 'x'")
    with pytest.raises(asyncpg.RaiseError):
        await database.execute("DELETE FROM audit_events")


async def test_audit_records_who_did_what_without_content(family):
    await family.upload("amma", pdf("a"), filename="Rahul fee reminder.pdf")
    assert (await family.client.get("/v1/audit", headers=family.h("paati"))).status_code == 403
    events = (await family.client.get("/v1/audit", headers=family.h("appa"))).json()
    actions = [e["action"] for e in events]
    assert actions[-1] == "household.created" and "artifact.received" in actions
    received = next(e for e in events if e["action"] == "artifact.received")
    assert received["actor_member_id"] == family.amma["id"]
    assert "Rahul" not in str(events)


async def test_rejected_uploads_are_audited(family):
    await family.upload("amma", b"\x00\x01binary", filename="x.exe")
    events = (await family.client.get("/v1/audit", headers=family.h("amma"))).json()
    assert events[0]["action"] == "intake.rejected" and events[0]["detail"] == {"reason": "unsupported_type"}
