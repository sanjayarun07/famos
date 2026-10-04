"""Doing something about a notice.

Two kinds only, and two properties that make them safe enough to ship: the
recipient is derived from the notice rather than supplied, and an approval is
bound to the exact words it was given for.
"""
import datetime as dt
import uuid

import pytest

from familyos import actions, mail
from familyos.db import pool
from familyos.identity import Invalid, NotFound, Principal
from familyos.settings import settings
from tests.conftest import pdf

TOMORROW = dt.date.today() + dt.timedelta(days=1)
SCHOOL = "office@school.example.com"


def _principal(family, who="amma"):
    m = getattr(family, who)
    return Principal(member_id=uuid.UUID(m["id"]), household_id=uuid.UUID(family.household["id"]),
                     role=m["role"], display_name=m["display_name"])


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Nothing leaves the test suite. Records what would have."""
    out = []

    async def fake(to, subject, body, *, ics=None):
        out.append({"to": to, "subject": subject, "body": body, "ics": ics})

    monkeypatch.setattr(mail, "send", fake)
    monkeypatch.setattr(mail, "configured", lambda: True)
    monkeypatch.setattr(settings, "actions_enabled", True)
    return out


async def _notice(database, family, *, channel="gmail", sender=SCHOOL, subject="Sports day",
                  title="Confirm attendance at sports day", due=TOMORROW, action="attend"):
    """A notice that arrived from a school, and an obligation read out of it."""
    household_id = uuid.UUID(family.household["id"])
    artifact_id = uuid.UUID((await family.upload("amma", pdf(sender + title))).json()["artifact"]["id"])
    await database.execute(
        "UPDATE input_artifacts SET channel = $2, visibility = 'shared', "
        "source = jsonb_build_object('from', $3::text, 'subject', $4::text) WHERE id = $1",
        artifact_id, channel, sender, subject)
    extraction_id, claim_id, obligation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await database.execute(
        "INSERT INTO extractions (id, household_id, artifact_id, extractor, prompt_version, parser_version, "
        "actionable, page_count) VALUES ($1, $2, $3, 'rules', 'v1', 'v1', TRUE, 1)",
        extraction_id, household_id, artifact_id)
    await database.execute(
        "INSERT INTO claims (id, extraction_id, household_id, artifact_id, ordinal, kind, title, quote, "
        "confidence) VALUES ($1, $2, $3, $4, 0, 'event', $5, 'the words', 0.9)",
        claim_id, extraction_id, household_id, artifact_id, title)
    await database.execute(
        "INSERT INTO obligations (id, household_id, artifact_id, claim_id, kind, action, title, due_date, status) "
        "VALUES ($1, $2, $3, $4, 'task', $5, $6, $7, 'accepted')",
        obligation_id, household_id, artifact_id, claim_id, action, title, due)
    return obligation_id, artifact_id


# ----------------------------------------------------------------------------
# the recipient is derived, never supplied
# ----------------------------------------------------------------------------

async def test_a_reply_goes_to_the_school_that_wrote(family, database, sent):
    obligation_id, _ = await _notice(database, family)
    a = await actions.propose(_principal(family), obligation_id, "reply", "Yes, both of us will be there.")
    assert a["recipient"] == SCHOOL
    assert a["subject"] == "Re: Sports day"
    assert a["status"] == "proposed"

    await actions.approve(_principal(family), a["id"], a["params_sha256"])
    assert (await actions.send_approved())["sent"] == 1
    assert sent[0]["to"] == SCHOOL
    assert "both of us" in sent[0]["body"]


async def test_there_is_no_way_to_address_a_reply_anywhere_else(family, database, sent):
    """The property that makes this shippable. A caller sends an obligation and
    a message; the address is not theirs to choose, at any layer."""
    import inspect

    from familyos.models import ActionIn
    assert "recipient" not in ActionIn.model_fields
    assert "to" not in ActionIn.model_fields
    assert "recipient" not in inspect.signature(actions.propose).parameters

    obligation_id, _ = await _notice(database, family)
    r = await family.client.post(f"/v1/obligations/{obligation_id}/actions", headers=family.h("amma"),
                                 json={"kind": "reply", "body": "Yes", "recipient": "attacker@evil.example"})
    assert r.status_code == 201, r.text
    assert r.json()["recipient"] == SCHOOL


async def test_a_forward_from_our_own_member_has_nobody_to_reply_to(family, database, sent):
    """Forwarded mail names the member who forwarded it, so replying would
    write back to the family rather than to the school."""
    obligation_id, _ = await _notice(database, family, channel="email", sender="amma@example.com")
    with pytest.raises(actions.NothingToReplyTo):
        await actions.propose(_principal(family), obligation_id, "reply", "Yes")


async def test_a_whatsapp_message_has_nobody_to_reply_to(family, database, sent):
    """A phone number is not an address, and a group message was written by
    another parent who did not ask to be answered."""
    obligation_id, _ = await _notice(database, family, channel="whatsapp", sender="919876543210")
    with pytest.raises(actions.NothingToReplyTo):
        await actions.propose(_principal(family), obligation_id, "reply", "Yes")


# ----------------------------------------------------------------------------
# the approval is bound to the exact words
# ----------------------------------------------------------------------------

async def test_approving_something_that_changed_is_refused(family, database, sent):
    """An approval is an approval of something, not a flag. If the stored
    action is not what the person read, it does not apply to it."""
    obligation_id, _ = await _notice(database, family)
    a = await actions.propose(_principal(family), obligation_id, "reply", "Yes, both of us.")

    # Something changes the body between showing and approving.
    await database.execute("UPDATE actions SET body = 'No, neither of us.' WHERE id = $1", a["id"])
    with pytest.raises(Invalid) as caught:
        await actions.approve(_principal(family), a["id"], a["params_sha256"])
    assert "changed since it was proposed" in str(caught.value)
    assert (await actions.send_approved())["sent"] == 0
    assert sent == []


async def test_approving_a_fingerprint_you_were_not_shown_is_refused(family, database, sent):
    """The other half: the stored action is intact, but the caller is echoing
    something else."""
    obligation_id, _ = await _notice(database, family)
    a = await actions.propose(_principal(family), obligation_id, "reply", "Yes")
    with pytest.raises(Invalid) as caught:
        await actions.approve(_principal(family), a["id"], "f" * 64)
    assert "not what you were shown" in str(caught.value)
    assert (await actions.send_approved())["sent"] == 0


async def test_the_fingerprint_covers_recipient_subject_and_body(family):
    base = actions.fingerprint("a@b.test", "Re: x", "yes")
    assert base != actions.fingerprint("c@d.test", "Re: x", "yes")
    assert base != actions.fingerprint("a@b.test", "Re: y", "yes")
    assert base != actions.fingerprint("a@b.test", "Re: x", "no")
    assert base == actions.fingerprint("a@b.test", "Re: x", "yes")


async def test_nothing_is_sent_until_somebody_approves_it(family, database, sent):
    obligation_id, _ = await _notice(database, family)
    await actions.propose(_principal(family), obligation_id, "reply", "Yes")
    assert (await actions.send_approved())["sent"] == 0
    assert sent == []


# ----------------------------------------------------------------------------
# what cannot be done at all
# ----------------------------------------------------------------------------

async def test_there_is_no_payment_kind(family, database):
    """The deny-list is a database constraint, so widening it is a migration
    somebody writes and reviews."""
    obligation_id, _ = await _notice(database, family)
    assert actions.KINDS == ("reply", "calendar")
    with pytest.raises(Invalid):
        await actions.propose(_principal(family), obligation_id, "payment", "₹3000")
    r = await family.client.post(f"/v1/obligations/{obligation_id}/actions", headers=family.h("amma"),
                                 json={"kind": "purchase", "body": "a lego set"})
    assert r.status_code == 422


async def test_a_superseded_obligation_is_not_answered(family, database, sent):
    """If a later notice changed the date, answering the old one is worse than
    not answering."""
    obligation_id, _ = await _notice(database, family)
    a = await actions.propose(_principal(family), obligation_id, "reply", "Yes")
    await actions.approve(_principal(family), a["id"], a["params_sha256"])
    await database.execute("UPDATE obligations SET superseded_at = NOW() WHERE id = $1", obligation_id)
    assert (await actions.send_approved())["sent"] == 0
    assert sent == []


async def test_proposing_twice_gives_one_thing_to_decide(family, database, sent):
    obligation_id, _ = await _notice(database, family)
    await actions.propose(_principal(family), obligation_id, "reply", "Yes")
    with pytest.raises(Invalid):
        await actions.propose(_principal(family), obligation_id, "reply", "Actually no")


async def test_an_action_on_a_private_notice_is_not_anybody_elses(family, database, sent):
    """The visibility rule again: it governs actions as it governs reads."""
    obligation_id, artifact_id = await _notice(database, family)
    await database.execute("UPDATE input_artifacts SET visibility = 'private', submitted_by = $2 WHERE id = $1",
                           artifact_id, uuid.UUID(family.amma["id"]))
    a = await actions.propose(_principal(family, "amma"), obligation_id, "reply", "Yes")
    with pytest.raises(NotFound):
        await actions.approve(_principal(family, "appa"), a["id"], a["params_sha256"])
    assert await actions.list_for(_principal(family, "appa")) == []
    assert len(await actions.list_for(_principal(family, "amma"))) == 1


# ----------------------------------------------------------------------------
# the calendar entry
# ----------------------------------------------------------------------------

async def test_a_dated_thing_becomes_a_calendar_entry(family, database, sent):
    """No calendar write scope, so no new permission from anybody: it goes to
    the member as an .ics a mail client offers to add."""
    obligation_id, _ = await _notice(database, family)
    a = await actions.propose(_principal(family), obligation_id, "calendar")
    assert a["recipient"] == "amma@example.com"
    await actions.approve(_principal(family), a["id"], a["params_sha256"])
    assert (await actions.send_approved())["sent"] == 1

    name, payload = sent[0]["ics"]
    text = payload.decode()
    assert name.endswith(".ics")
    assert "BEGIN:VEVENT" in text and "END:VCALENDAR" in text
    assert f"DTSTART;VALUE=DATE:{TOMORROW.strftime('%Y%m%d')}" in text
    # DTEND is exclusive, so a one-day event ends the next day.
    assert f"DTEND;VALUE=DATE:{(TOMORROW + dt.timedelta(days=1)).strftime('%Y%m%d')}" in text
    assert "Confirm attendance at sports day" in text


async def test_an_undated_thing_has_no_calendar_entry(family, database, sent):
    obligation_id, _ = await _notice(database, family, due=None)
    with pytest.raises(Invalid):
        await actions.propose(_principal(family), obligation_id, "calendar")


async def test_a_title_with_a_comma_does_not_break_the_calendar(family, database, sent):
    """iCalendar escaping: a comma or semicolon in a title is a field
    separator unless escaped, and school notices are full of them."""
    obligation_id, _ = await _notice(database, family, title="Bring: a hat, water; and sunscreen")
    a = await actions.propose(_principal(family), obligation_id, "calendar")
    await actions.approve(_principal(family), a["id"], a["params_sha256"])
    await actions.send_approved()
    text = sent[0]["ics"][1].decode()
    assert r"Bring: a hat\, water\; and sunscreen" in text


# ----------------------------------------------------------------------------
# sending once
# ----------------------------------------------------------------------------

async def test_two_sweeps_do_not_send_the_same_action_twice(family, database, sent):
    import asyncio
    obligation_id, _ = await _notice(database, family)
    a = await actions.propose(_principal(family), obligation_id, "reply", "Yes")
    await actions.approve(_principal(family), a["id"], a["params_sha256"])
    first, second = await asyncio.gather(actions.send_approved(), actions.send_approved())
    assert first["sent"] + second["sent"] == 1
    assert len(sent) == 1


async def test_a_failed_send_is_retried_then_given_up_on(family, database, monkeypatch, sent):
    monkeypatch.setattr(settings, "action_max_attempts", 2)

    async def explode(*a, **kw):
        raise RuntimeError("the mail server is down")

    monkeypatch.setattr(mail, "send", explode)
    obligation_id, _ = await _notice(database, family)
    a = await actions.propose(_principal(family), obligation_id, "reply", "Yes")
    await actions.approve(_principal(family), a["id"], a["params_sha256"])

    assert (await actions.send_approved())["failed"] == 1
    row = await database.fetchrow("SELECT status, attempts, claimed_at FROM actions WHERE id = $1", a["id"])
    assert (row["status"], row["attempts"], row["claimed_at"]) == ("approved", 1, None)

    assert (await actions.send_approved())["failed"] == 1
    row = await database.fetchrow("SELECT status, attempts, error FROM actions WHERE id = $1", a["id"])
    assert row["status"] == "failed" and row["attempts"] == 2 and "mail server" in row["error"]


async def test_the_audit_trail_records_who_approved_what_without_the_words(family, database, sent):
    obligation_id, _ = await _notice(database, family)
    a = await actions.propose(_principal(family), obligation_id, "reply", "Yes, both of us will be there.")
    await actions.approve(_principal(family), a["id"], a["params_sha256"])
    await actions.send_approved()

    rows = await database.fetch(
        "SELECT action, actor_member_id, detail FROM audit_events WHERE action LIKE 'action.%' ORDER BY at")
    assert [r["action"] for r in rows] == ["action.proposed", "action.approved", "action.sent"]
    blob = " ".join(str(r["detail"]) for r in rows)
    # The domain, so a reader can see where it went; never the words.
    assert "school.example.com" in blob
    assert "both of us" not in blob
    assert SCHOOL not in blob
    assert rows[2]["detail"]["approved_by"] == family.amma["id"]


async def test_nothing_sends_while_actions_are_off(family, database, monkeypatch, sent):
    monkeypatch.setattr(settings, "actions_enabled", False)
    assert "off" in actions.describe()
    monkeypatch.setattr(settings, "actions_enabled", True)
    monkeypatch.setattr(mail, "configured", lambda: False)
    assert "MISCONFIGURED" in actions.describe()


async def test_the_whole_flow_over_http(family, database, sent):
    obligation_id, _ = await _notice(database, family)
    r = await family.client.post(f"/v1/obligations/{obligation_id}/actions", headers=family.h("amma"),
                                 json={"kind": "reply", "body": "Yes, both of us will be there."})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["recipient"] == SCHOOL

    bad = await family.client.post(f"/v1/actions/{a['id']}/approve", headers=family.h("amma"),
                                   json={"params_sha256": "0" * 64})
    assert bad.status_code == 422

    ok = await family.client.post(f"/v1/actions/{a['id']}/approve", headers=family.h("amma"),
                                  json={"params_sha256": a["params_sha256"]})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"

    async with pool().acquire():
        pass
    assert (await actions.send_approved())["sent"] == 1
    listed = (await family.client.get("/v1/actions", headers=family.h("amma"))).json()
    assert listed[0]["status"] == "sent"
