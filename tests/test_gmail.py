"""Connecting a parent's Gmail, and what the stored token is worth on its own.

Nothing here touches the network: `transport` is injected, so these are about
what FamilyOS does with what Google says rather than about Google.
"""
import base64
import datetime as dt
import json
import uuid
from email.message import EmailMessage

import pytest
from cryptography.exceptions import InvalidTag

from familyos import crypto
from familyos.identity import Invalid
from familyos.intake import google, mailbox
from familyos.settings import settings

REFRESH = "1//refresh-token-value"


@pytest.fixture(autouse=True)
def oauth_client(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(settings, "google_redirect_uri", "http://localhost:8000/v1/google/callback")
    monkeypatch.setattr(settings, "gmail_query", "subject:school")


def _id_token(email="amma@gmail.com", sub="google-sub-1") -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return seg({"alg": "none"}) + "." + seg({"email": email, "sub": sub}) + ".sig"


def _raw(subject="Swimming on Friday", sender="office@school.example.com", attach=None) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "amma@gmail.com"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid.uuid4()}@school.example.com>"
    msg["Authentication-Results"] = "mx.google.com; dkim=pass header.d=school.example.com; dmarc=pass"
    msg.set_content("Please return the slip by Friday.")
    if attach:
        msg.add_attachment(attach, maintype="application", subtype="pdf", filename="circular.pdf")
    return msg.as_bytes()


class FakeGoogle:
    """Google, as far as these tests are concerned. Records what was asked."""

    def __init__(self, *, messages=None, refresh_token=REFRESH, token_status=200, email="amma@gmail.com",
                 sub="google-sub-1", scopes=None):
        self.messages = messages or {}
        self.refresh_token = refresh_token
        self.token_status = token_status
        self.email = email
        self.sub = sub
        self.scopes = scopes if scopes is not None else list(google.SCOPES)
        self.queries: list[str] = []
        self.revoked: list[str] = []
        self.fetched: list[str] = []

    async def __call__(self, method, url, *, headers, params, data):
        if url == google.TOKEN_URL:
            if self.token_status != 200:
                return self.token_status, b'{"error": "invalid_grant"}'
            body = {"access_token": "access-token", "expires_in": 3599,
                    "scope": " ".join(self.scopes), "id_token": _id_token(self.email, self.sub)}
            if data.get("grant_type") == "authorization_code" and self.refresh_token:
                body["refresh_token"] = self.refresh_token
            return 200, json.dumps(body).encode()
        if url == google.REVOKE_URL:
            self.revoked.append(data.get("token"))
            return 200, b""
        if url.endswith("/profile"):
            return 200, json.dumps({"emailAddress": self.email, "historyId": "99"}).encode()
        if url.endswith("/messages"):
            self.queries.append(params.get("q", ""))
            listed = [{"id": k, "threadId": "t-" + k} for k in self.messages]
            return 200, json.dumps({"messages": listed}).encode()
        if "/messages/" in url:
            message_id = url.rsplit("/", 1)[1]
            self.fetched.append(message_id)
            raw = self.messages[message_id]
            return 200, json.dumps({"raw": base64.urlsafe_b64encode(raw).decode()}).encode()
        raise AssertionError("unexpected call to " + url)


async def _connect(family, fake, who="amma"):
    begun = await mailbox.begin(_principal(family, who))
    state = begun["state"]
    return await mailbox.complete(state, "auth-code", transport=fake)


def _principal(family, who="amma"):
    from familyos.identity import Principal
    member = getattr(family, who)
    return Principal(member_id=uuid.UUID(member["id"]), household_id=uuid.UUID(family.household["id"]),
                     role=member["role"], display_name=member["display_name"])


# ----------------------------------------------------------------------------
# asking for access
# ----------------------------------------------------------------------------

def test_the_consent_url_asks_for_read_only_and_a_refresh_token():
    verifier, challenge = google.pkce()
    url = google.authorize_url(client_id="cid", redirect_uri="http://localhost/cb",
                               state="st", challenge=challenge)
    assert "gmail.readonly" in url
    # Nothing that could change or send mail.
    assert "gmail.modify" not in url and "gmail.send" not in url
    # Without these two there is no refresh token, and the poller can never
    # run unattended.
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "code_challenge_method=S256" in url and challenge in url
    assert verifier not in url


async def test_a_callback_we_did_not_start_is_refused(family, database):
    fake = FakeGoogle()
    with pytest.raises(Invalid):
        await mailbox.complete("not-a-state-we-issued", "auth-code", transport=fake)
    assert await database.fetchval("SELECT count(*) FROM google_accounts") == 0


async def test_a_state_is_single_use(family, database):
    """It is the only thing tying Google's redirect to a member, so it cannot
    be replayed."""
    fake = FakeGoogle()
    begun = await mailbox.begin(_principal(family))
    await mailbox.complete(begun["state"], "auth-code", transport=fake)
    with pytest.raises(Invalid):
        await mailbox.complete(begun["state"], "auth-code", transport=fake)


async def test_a_connection_without_a_refresh_token_is_refused(family, database):
    """It would look like it worked and read nothing after an hour."""
    fake = FakeGoogle(refresh_token=None)
    begun = await mailbox.begin(_principal(family))
    with pytest.raises(Invalid):
        await mailbox.complete(begun["state"], "auth-code", transport=fake)
    assert await database.fetchval("SELECT count(*) FROM google_accounts") == 0


async def test_a_connection_without_gmail_access_is_refused(family, database):
    """Google lets the member untick a scope. Without this one there is
    nothing to read."""
    fake = FakeGoogle(scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"])
    begun = await mailbox.begin(_principal(family))
    with pytest.raises(Invalid):
        await mailbox.complete(begun["state"], "auth-code", transport=fake)


# ----------------------------------------------------------------------------
# what is stored
# ----------------------------------------------------------------------------

async def test_the_refresh_token_is_sealed_with_the_household_key(family, database):
    fake = FakeGoogle()
    await _connect(family, fake)
    row = await database.fetchrow("SELECT * FROM google_accounts")
    sealed = bytes(row["sealed_refresh_token"])
    assert REFRESH.encode() not in sealed

    household_id, member_id = row["household_id"], row["member_id"]
    wrapped = await database.fetchval("SELECT wrapped_key FROM households WHERE id = $1", household_id)
    key = crypto.unwrap(household_id, wrapped)
    aad = household_id.bytes + member_id.bytes + b"google-refresh"
    assert crypto.open_sealed(key, sealed, aad).decode() == REFRESH
    # Bound to the member: the same ciphertext under anyone else does not open.
    with pytest.raises(InvalidTag):
        crypto.open_sealed(key, sealed, household_id.bytes + uuid.uuid4().bytes + b"google-refresh")


async def test_the_api_never_returns_the_token(family, database):
    fake = FakeGoogle()
    await _connect(family, fake)
    r = await family.client.get("/v1/google/mailboxes", headers=family.h("amma"))
    assert r.status_code == 200, r.text
    body = r.text
    assert "amma@gmail.com" in body
    assert REFRESH not in body and "sealed" not in body


async def test_the_audit_trail_records_the_address_and_not_the_token(family, database):
    fake = FakeGoogle()
    await _connect(family, fake)
    detail = await database.fetchval(
        "SELECT detail FROM audit_events WHERE action = 'mailbox.connected'")
    assert detail["email"] == "amma@gmail.com"
    assert REFRESH not in json.dumps(detail)


async def test_nobody_sees_another_members_mailbox(family, database):
    """A mailbox is more private than a notice out of it, so not even a
    guardian sees somebody else's."""
    await _connect(family, FakeGoogle(), who="amma")
    assert len((await family.client.get("/v1/google/mailboxes", headers=family.h("amma"))).json()) == 1
    assert (await family.client.get("/v1/google/mailboxes", headers=family.h("appa"))).json() == []


async def test_reconnecting_the_same_mailbox_replaces_it(family, database):
    await _connect(family, FakeGoogle())
    await _connect(family, FakeGoogle(refresh_token="1//a-newer-token"))
    assert await database.fetchval("SELECT count(*) FROM google_accounts WHERE disconnected_at IS NULL") == 1


# ----------------------------------------------------------------------------
# reading it
# ----------------------------------------------------------------------------

async def test_a_poll_stores_what_it_finds_and_only_once(family, database):
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    fake = FakeGoogle(messages={"m1": _raw(attach=pdf), "m2": _raw(subject="Fees due")})
    account = await _connect(family, fake)

    first = await mailbox.poll(account["id"], transport=fake)
    assert first["stored"] == 2 and first["duplicates"] == 0
    # The mail itself, plus the attachment as a child of it.
    channels = {r["channel"]: r["count"] for r in await database.fetch(
        "SELECT channel, count(*) AS count FROM input_artifacts GROUP BY channel")}
    assert channels["gmail"] == 2
    assert channels["gmail_attachment"] == 1

    # The same window again stores nothing new: Gmail's own id is the dedup key.
    second = await mailbox.poll(account["id"], transport=fake)
    assert second["stored"] == 0 and second["duplicates"] == 2


async def test_the_mail_belongs_to_the_member_who_connected_the_mailbox(family, database):
    """The sender is the school, not the member. Matching on sender would
    refuse every notice; the member *received* it, so it is stored for them,
    privately."""
    fake = FakeGoogle(messages={"m1": _raw()})
    account = await _connect(family, fake)
    await mailbox.poll(account["id"], transport=fake)
    row = await database.fetchrow("SELECT submitted_by, visibility, source FROM input_artifacts WHERE channel = 'gmail'")
    assert row["submitted_by"] == uuid.UUID(family.amma["id"])
    assert row["visibility"] == "private"
    assert row["source"]["from"] == "office@school.example.com"
    assert row["source"]["gmail_id"] == "m1"


async def test_the_first_pass_reaches_back_and_later_polls_do_not(family, database):
    fake = FakeGoogle(messages={"m1": _raw()})
    account = await _connect(family, fake)
    await mailbox.poll(account["id"], transport=fake)
    first_query = fake.queries[0]
    assert "subject:school" in first_query and "after:" in first_query

    # The backfill finished (one page, no next token), so the next poll asks
    # from the last poll instead of from the backfill window.
    assert await database.fetchval("SELECT backfill_done FROM google_accounts WHERE id = $1", account["id"])
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    await mailbox.poll(account["id"], now=later, transport=fake)
    assert fake.queries[1] != first_query


def test_coming_back_for_a_poll_overlaps_the_last_one():
    """An hour of overlap, so a late delivery or a clock skew cannot drop a
    message. Overlap costs nothing: the id has already been stored."""
    now = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.UTC)
    since = now - dt.timedelta(minutes=30)
    query = google.query_for("subject:school", since=since, backfill_days=30, now=now)
    asked_from = dt.datetime.fromtimestamp(int(query.split("after:")[1]), dt.UTC)
    assert asked_from == since - google.OVERLAP
    # The base query is parenthesised, or an OR inside it would swallow the date.
    assert query.startswith("(subject:school)")


# ----------------------------------------------------------------------------
# when it stops working
# ----------------------------------------------------------------------------

async def test_a_revoked_connection_asks_to_be_reconnected(family, database):
    """Google answers invalid_grant once the member takes access away -- or,
    in an OAuth client still in testing, after seven days. Only they can fix
    it, so the poller stops asking and says so."""
    fake = FakeGoogle(messages={"m1": _raw()})
    account = await _connect(family, fake)
    fake.token_status = 400

    assert (await mailbox.poll(account["id"], transport=fake))["skipped"] == "needs_reconnect"
    row = await database.fetchrow("SELECT needs_reconnect, last_error FROM google_accounts WHERE id = $1",
                                  account["id"])
    assert row["needs_reconnect"] and row["last_error"]
    # And it is not offered to the sweep again.
    assert account["id"] not in await mailbox.due()


async def test_erasing_the_household_ends_access_to_the_mailbox(family, database):
    """The point of sealing it with the household key: erasure destroys that
    key first, so the token becomes unreadable without anyone remembering to
    revoke it."""
    fake = FakeGoogle(messages={"m1": _raw()})
    account = await _connect(family, fake)
    await database.execute("UPDATE households SET status = 'erasing' WHERE id = $1",
                           uuid.UUID(family.household["id"]))

    assert (await mailbox.poll(account["id"], transport=fake))["skipped"] == "unreadable_token"
    assert await database.fetchval("SELECT needs_reconnect FROM google_accounts WHERE id = $1", account["id"])


async def test_disconnecting_overwrites_the_token_and_tells_google(family, database):
    fake = FakeGoogle()
    account = await _connect(family, fake)
    gone = await mailbox.disconnect(_principal(family), account["id"], transport=fake)
    assert gone["disconnected_at"] is not None
    assert fake.revoked == [REFRESH]
    sealed = await database.fetchval("SELECT sealed_refresh_token FROM google_accounts WHERE id = $1",
                                      account["id"])
    assert bytes(sealed) == b""
    assert await mailbox.list_for(_principal(family)) == []


async def test_only_the_member_whose_mailbox_it_is_may_disconnect_it(family, database):
    from familyos.identity import NotFound
    account = await _connect(family, FakeGoogle(), who="amma")
    with pytest.raises(NotFound):
        await mailbox.disconnect(_principal(family, "appa"), account["id"], transport=FakeGoogle())


async def test_without_an_oauth_client_connecting_says_so_plainly(family, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", None)
    with pytest.raises(mailbox.NotConfigured):
        await mailbox.begin(_principal(family))
    assert "MISCONFIGURED" not in mailbox.describe()  # off, not broken


async def test_a_sweep_only_takes_live_mailboxes_of_live_households(family, database):
    fake = FakeGoogle(messages={"m1": _raw()})
    account = await _connect(family, fake)
    assert account["id"] in await mailbox.due()
    await database.execute("UPDATE households SET status = 'erasing' WHERE id = $1",
                           uuid.UUID(family.household["id"]))
    assert await mailbox.due() == []


async def test_the_poll_interval_is_respected_once_the_backfill_is_done(family, database, monkeypatch):
    monkeypatch.setattr(settings, "gmail_poll_seconds", 300)
    fake = FakeGoogle(messages={"m1": _raw()})
    account = await _connect(family, fake)
    await mailbox.poll(account["id"], transport=fake)
    # Just polled, so not due again yet.
    assert await mailbox.due() == []
    assert account["id"] in await mailbox.due(now=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=301))


# ----------------------------------------------------------------------------
# not losing one
# ----------------------------------------------------------------------------

async def test_a_message_that_might_store_later_is_not_stepped_past(family, database, monkeypatch):
    """The cursor used to move whatever happened, so one transient storage
    failure lost a notice for good: the backfill never returns to a page it
    has left, and the incremental window closes behind it."""
    fake = FakeGoogle(messages={"m1": _raw(), "m2": _raw(subject="Fees")})
    account = await _connect(family, fake)

    real = mailbox.gateway.receive_gmail
    hit = {"n": 0}

    async def flaky(*a, **kw):
        hit["n"] += 1
        if kw.get("gmail_id") == "m2" and hit["n"] < 3:
            raise RuntimeError("the object store blinked")
        return await real(*a, **kw)

    monkeypatch.setattr(mailbox.gateway, "receive_gmail", flaky)
    first = await mailbox.poll(account["id"], transport=fake)
    assert first["stored"] == 1 and first["failed"] == 1 and first["stalled"] is True
    row = await database.fetchrow(
        "SELECT last_polled_at, backfill_done, last_error FROM google_accounts WHERE id = $1", account["id"])
    # Nothing moved, and it says why.
    assert row["last_polled_at"] is None and row["backfill_done"] is False
    assert "could not be stored" in row["last_error"]

    # So the next pass sees it again, and this time it works.
    second = await mailbox.poll(account["id"], transport=fake)
    assert second["stored"] == 1 and second["failed"] == 0 and second["stalled"] is False
    assert await database.fetchval("SELECT count(*) FROM input_artifacts WHERE channel = 'gmail'") == 2
    row = await database.fetchrow(
        "SELECT last_polled_at, last_error FROM google_accounts WHERE id = $1", account["id"])
    assert row["last_polled_at"] is not None and row["last_error"] is None


async def test_a_message_that_can_never_be_stored_is_stepped_past(family, database, monkeypatch):
    """Too large, or nothing readable in it. The next poll would refuse it
    identically, so refusing is not a reason to stop."""
    fake = FakeGoogle(messages={"m1": _raw()})
    account = await _connect(family, fake)

    async def refuse(*a, **kw):
        raise mailbox.gateway.Rejected("too_large", status=413)

    monkeypatch.setattr(mailbox.gateway, "receive_gmail", refuse)
    result = await mailbox.poll(account["id"], transport=fake)
    assert result["refused"] == 1 and result["failed"] == 0 and result["stalled"] is False
    assert await database.fetchval("SELECT last_polled_at FROM google_accounts WHERE id = $1",
                                    account["id"]) is not None


async def test_two_members_can_connect_the_same_mailbox(family, database):
    """A shared family Gmail. Keying only on Gmail's id gave the first poller
    a private copy and the second nothing to see."""
    shared = FakeGoogle(messages={"m1": _raw()}, email="family@gmail.com", sub="shared-sub")
    amma = await _connect(family, shared, who="amma")
    appa = await _connect(family, FakeGoogle(messages={"m1": _raw()}, email="family@gmail.com",
                                             sub="shared-sub"), who="appa")
    assert amma["id"] != appa["id"]

    await mailbox.poll(amma["id"], transport=shared)
    await mailbox.poll(appa["id"], transport=shared)

    owners = {r["submitted_by"] for r in await database.fetch(
        "SELECT submitted_by FROM input_artifacts WHERE channel = 'gmail'")}
    assert owners == {uuid.UUID(family.amma["id"]), uuid.UUID(family.appa["id"])}
    # And each of them can see their own.
    for who in ("amma", "appa"):
        seen = (await family.client.get("/v1/artifacts", headers=family.h(who))).json()
        assert [a for a in seen if a["channel"] == "gmail"]


async def test_a_mailbox_that_has_gone_quiet_says_so_however_it_went_quiet(family, database):
    """needs_reconnect is one cause of not being read. A wedged message is
    another, and a poller that is not running is a third. The symptom a family
    cares about is the same."""
    fake = FakeGoogle(messages={"m1": _raw()})
    account = await _connect(family, fake)
    live = (await mailbox.list_for(_principal(family)))[0]
    assert mailbox.is_quiet(live) is False

    await database.execute("UPDATE google_accounts SET connected_at = NOW() - INTERVAL '3 days' WHERE id = $1",
                           account["id"])
    stale = (await mailbox.list_for(_principal(family)))[0]
    assert mailbox.is_quiet(stale) is True
