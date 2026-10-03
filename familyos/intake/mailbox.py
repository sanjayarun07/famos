"""Connected mailboxes: the state, and the sweep that reads them.

`google.py` speaks the protocol and holds no state. This holds the state: who
connected what, the sealed token that lets us keep reading it, and how far each
mailbox has been read.

The refresh token is the whole of the access, so it is sealed with the
household's data key -- the same key the originals are sealed with, bound to
this member by the AAD. Two things follow, and both matter more than the
convenience:

- Erasing a household destroys that key first, so erasure also ends FamilyOS's
  access to every mailbox the household had connected. Nothing has to remember
  to go and revoke anything, and a backup of this table is no more useful than
  a backup of the blobs.
- A database dump on its own is not access. It takes the master key as well.

Access tokens are never stored. Each poll refreshes one, uses it, and drops
it: an hour-long credential at rest buys nothing and is one more thing to
erase.
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets
import uuid

import asyncpg

from familyos import artifacts, audit, crypto, jobs
from familyos.db import pool
from familyos.identity import Invalid, NotFound, Principal
from familyos.intake import gateway, google
from familyos.settings import settings

logger = logging.getLogger(__name__)

KIND = "poll_mailboxes"
HANDLER_VERSION = "1"

# An authorisation left half-finished is not worth keeping: the member can
# always start again, and a stale state row is a replay waiting to happen.
STATE_TTL = dt.timedelta(minutes=10)

# A connection that has not managed a clean pass in this long has gone quiet,
# whatever the reason -- a dead token, a message it cannot get past, a poller
# that is not running. The cause differs; the symptom a family cares about is
# the same, and it is the one worth telling them about.
QUIET_AFTER = dt.timedelta(hours=24)


class NotConfigured(RuntimeError):
    """No OAuth client. Connecting is impossible until one exists, and saying
    so plainly beats a redirect to a Google error page."""


def configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def _require_client() -> tuple[str, str]:
    if not configured():
        raise NotConfigured(
            "FAMILYOS_GOOGLE_CLIENT_ID and FAMILYOS_GOOGLE_CLIENT_SECRET are not set, so no mailbox can be "
            "connected. Create an OAuth client in a Google Cloud project, add "
            f"{settings.google_redirect_uri} as a redirect URI, and put the two values in the environment.")
    return settings.google_client_id, settings.google_client_secret


def _aad(household_id: uuid.UUID, member_id: uuid.UUID) -> bytes:
    """Binds the sealed token to the member it belongs to, so a row moved to
    another member does not decrypt."""
    return household_id.bytes + member_id.bytes + b"google-refresh"


# ----------------------------------------------------------------------------
# connecting
# ----------------------------------------------------------------------------

async def begin(p: Principal) -> dict:
    """Where to send the member, and the state that proves the callback is
    the answer to this request."""
    client_id, _ = _require_client()
    state = secrets.token_urlsafe(32)
    verifier, challenge = google.pkce()
    async with pool().acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM google_auth_states WHERE created_at < NOW() - $1::interval", STATE_TTL)
        await conn.execute(
            "INSERT INTO google_auth_states (state, household_id, member_id, code_verifier) VALUES ($1, $2, $3, $4)",
            state, p.household_id, p.member_id, verifier)
    return {"state": state,
            "url": google.authorize_url(client_id=client_id, redirect_uri=settings.google_redirect_uri,
                                        state=state, challenge=challenge)}


async def complete(state: str, code: str, *, transport=None) -> dict:
    """Finish an authorisation. The state row says whose it is -- the callback
    arrives from Google with no FamilyOS token on it, so this is the only
    thing tying the consent to a member, which is why it is single-use."""
    client_id, client_secret = _require_client()
    async with pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM google_auth_states WHERE state = $1 AND used_at IS NULL "
            "AND created_at > NOW() - $2::interval FOR UPDATE", state, STATE_TTL)
        if row is None:
            raise Invalid("this authorisation is not one we started, or it has expired")
        await conn.execute("UPDATE google_auth_states SET used_at = NOW() WHERE state = $1", state)
    household_id, member_id = row["household_id"], row["member_id"]

    token = await google.exchange_code(code, row["code_verifier"], client_id=client_id,
                                       client_secret=client_secret,
                                       redirect_uri=settings.google_redirect_uri, transport=transport)
    if not token.refresh_token:
        # Without one the poller can never run unattended, and a connection
        # that reads nothing is worse than none: it looks like it works.
        raise Invalid("Google did not return a refresh token. Remove FamilyOS from the account's third-party "
                      "access and connect again so it asks for consent afresh.")
    missing = google.missing_scopes(token.scopes)
    if any("gmail.readonly" in scope for scope in missing):
        raise Invalid("Read-only access to Gmail was not granted, so there is nothing to read. Connect again and "
                      "leave that permission ticked.")

    # Ask the mailbox who it is, so the member can tell which one this is, and
    # take Gmail's cursor while we are there.
    me = await google.profile(token.access_token, transport=transport)
    email = me.get("emailAddress") or token.email
    subject = token.subject or email
    if not email or not subject:
        raise Invalid("Google did not say which account this is")

    async with pool().acquire() as conn, conn.transaction():
        key = await artifacts.household_key(conn, household_id, lock=True)
        sealed = crypto.seal(key, token.refresh_token.encode(), _aad(household_id, member_id))
        account = await conn.fetchrow(
            "INSERT INTO google_accounts (id, household_id, member_id, email, subject, scopes, "
            "sealed_refresh_token, history_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            # Reconnecting the same mailbox replaces the token and starts its
            # first pass again; it does not make a second row.
            "ON CONFLICT (member_id, subject) WHERE disconnected_at IS NULL DO UPDATE SET "
            "email = EXCLUDED.email, scopes = EXCLUDED.scopes, "
            "sealed_refresh_token = EXCLUDED.sealed_refresh_token, history_id = EXCLUDED.history_id, "
            "needs_reconnect = FALSE, last_error = NULL, backfill_cursor = NULL, backfill_done = FALSE, "
            "connected_at = NOW() RETURNING *",
            uuid.uuid4(), household_id, member_id, email, subject, list(token.scopes), sealed,
            int(me["historyId"]) if me.get("historyId") else None)
        await audit.record(household_id, "mailbox.connected", actor_member_id=member_id,
                           target_type="google_account", target_id=account["id"],
                           # The address is the point of the record; the token never appears anywhere.
                           detail={"email": email, "scopes": list(token.scopes)}, conn=conn)
    return _public(account)


async def disconnect(p: Principal, account_id: uuid.UUID, *, transport=None) -> dict:
    """Stop reading, and tell Google so. The member's own connection only:
    a guardian cannot disconnect somebody else's mailbox, the same way a
    guardian cannot read somebody else's private notices."""
    async with pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM google_accounts WHERE id = $1 AND member_id = $2 AND disconnected_at IS NULL FOR UPDATE",
            account_id, p.member_id)
        if row is None:
            raise NotFound("connected mailbox")
        token = await _open_token(conn, row)
        account = await conn.fetchrow(
            # The sealed token is overwritten rather than kept: there is no
            # reason to be able to read that mailbox again.
            "UPDATE google_accounts SET disconnected_at = NOW(), sealed_refresh_token = '\\x', "
            "history_id = NULL, backfill_cursor = NULL WHERE id = $1 RETURNING *", account_id)
        await audit.record(p.household_id, "mailbox.disconnected", actor_member_id=p.member_id,
                           target_type="google_account", target_id=account_id,
                           detail={"email": row["email"]}, conn=conn)
    if token:
        try:
            await google.revoke(token, transport=transport)
        except Exception:
            # Ours is gone either way; Google's copy expires on its own.
            logger.warning("could not revoke a disconnected mailbox's token at Google", exc_info=True)
    return _public(account)


async def list_for(p: Principal) -> list[dict]:
    rows = await pool().fetch(
        "SELECT * FROM google_accounts WHERE member_id = $1 AND disconnected_at IS NULL ORDER BY connected_at",
        p.member_id)
    return [_public(r) for r in rows]


def is_quiet(account: dict, *, now: dt.datetime | None = None) -> bool:
    """Is this mailbox not being read? Asked of the shape _public returns, so
    the brief and the console agree on the answer."""
    if account.get("disconnected_at"):
        return False
    if account.get("needs_reconnect"):
        return True
    now = now or dt.datetime.now(dt.UTC)
    last = account.get("last_polled_at") or account.get("connected_at")
    return bool(last and now - last > QUIET_AFTER)


def _public(row) -> dict:
    """Everything but the token. There is no endpoint, for anybody, that
    returns the token."""
    return {"id": row["id"], "email": row["email"], "scopes": list(row["scopes"] or ()),
            "connected_at": row["connected_at"], "last_polled_at": row["last_polled_at"],
            "needs_reconnect": row["needs_reconnect"], "last_error": row["last_error"],
            "backfill_done": row["backfill_done"], "disconnected_at": row["disconnected_at"]}


async def _open_token(conn: asyncpg.Connection, row) -> str | None:
    sealed = row["sealed_refresh_token"]
    if not sealed:
        return None
    try:
        key = await artifacts.household_key(conn, row["household_id"])
    except Exception:
        # The household is being erased, or its key is already gone. Then so
        # is the access, which is the point.
        return None
    try:
        return crypto.open_sealed(key, sealed, _aad(row["household_id"], row["member_id"])).decode()
    except Exception:
        logger.warning("a connected mailbox's token would not open; it will be marked for reconnection")
        return None


# ----------------------------------------------------------------------------
# reading
# ----------------------------------------------------------------------------

async def poll(account_id: uuid.UUID, *, now: dt.datetime | None = None, transport=None) -> dict:
    """One mailbox, one pass. Returns what it did, so the job's result says
    something a person can read."""
    now = now or dt.datetime.now(dt.UTC)
    client_id, client_secret = _require_client()
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM google_accounts WHERE id = $1 AND disconnected_at IS NULL AND NOT needs_reconnect",
            account_id)
        if row is None:
            return {"skipped": "gone"}
        refresh_token = await _open_token(conn, row)
    if not refresh_token:
        await _mark_reconnect(account_id, "the stored token could not be read")
        return {"skipped": "unreadable_token"}

    try:
        token = await google.refresh(refresh_token, client_id=client_id, client_secret=client_secret,
                                     transport=transport)
    except google.NeedsReconnect as exc:
        await _mark_reconnect(account_id, str(exc))
        return {"skipped": "needs_reconnect"}

    query = google.query_for(settings.gmail_query,
                             since=None if not row["backfill_done"] else row["last_polled_at"],
                             backfill_days=settings.gmail_backfill_days, now=now)
    found, next_page = await google.list_messages(
        token.access_token, query=query, limit=max(1, settings.gmail_max_per_poll),
        page_token=row["backfill_cursor"] if not row["backfill_done"] else None, transport=transport)

    stored = duplicates = refused = failed = 0
    for summary in found:
        try:
            raw = await google.get_raw(token.access_token, summary.message_id, transport=transport)
            _, duplicate = await gateway.receive_gmail(
                row["household_id"], row["member_id"], raw,
                gmail_id=summary.message_id, thread_id=summary.thread_id)
        except gateway.Rejected as exc:
            # Permanent: too large, or nothing readable in it. The next poll
            # would refuse it identically, so it is counted and stepped past.
            logger.info("gmail message %s not taken: %s", summary.message_id, exc.reason)
            refused += 1
        except google.GoogleError:
            raise
        except Exception:
            # Storage, the database, a bug: as far as anything here can tell,
            # this might work next time. So it is NOT stepped past.
            logger.warning("gmail message %s could not be stored", summary.message_id, exc_info=True)
            failed += 1
        else:
            duplicates += 1 if duplicate else 0
            stored += 0 if duplicate else 1

    # Only move the cursor when nothing might still be retrievable. Advancing
    # over a transient failure loses the notice silently and for good -- the
    # backfill never returns to a page it has left, and the incremental window
    # closes behind it. Re-reading a page costs nothing, because a Gmail id
    # already stored is refused as a duplicate.
    #
    # The cost of that choice is that a message which fails for ever stalls
    # this mailbox. That is deliberate: a stalled mailbox says so in
    # last_error and in the brief, and a silently lossy one says nothing at
    # all. Of the two, only one can be noticed and fixed.
    stalled = failed > 0
    async with pool().acquire() as conn:
        if stalled:
            await conn.execute(
                "UPDATE google_accounts SET last_error = $2 WHERE id = $1", account_id,
                f"{failed} message(s) could not be stored; not moving on until they can"[:500])
        else:
            # The first pass walks pages until Gmail runs out; after that each
            # poll asks only for what is new, so the cursor is finished with.
            await conn.execute(
                "UPDATE google_accounts SET last_polled_at = $2, last_error = NULL, "
                "backfill_cursor = $3, backfill_done = $4 WHERE id = $1",
                account_id, now, next_page if not row["backfill_done"] else None,
                row["backfill_done"] or next_page is None)
    return {"email": row["email"], "looked_at": len(found), "stored": stored,
            "duplicates": duplicates, "refused": refused, "failed": failed, "stalled": stalled,
            "backfilling": not (row["backfill_done"] or next_page is None)}


async def _mark_reconnect(account_id: uuid.UUID, reason: str) -> None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE google_accounts SET needs_reconnect = TRUE, last_error = $2 WHERE id = $1 "
            "RETURNING household_id, member_id, email", account_id, reason[:500])
        if row is not None:
            await audit.record(row["household_id"], "mailbox.needs_reconnect", actor_kind="system",
                               target_type="google_account", target_id=account_id,
                               detail={"email": row["email"], "reason": reason[:200]}, conn=conn)


async def due(limit: int = 50, *, now: dt.datetime | None = None) -> list[uuid.UUID]:
    now = now or dt.datetime.now(dt.UTC)
    every = dt.timedelta(seconds=max(60, settings.gmail_poll_seconds))
    rows = await pool().fetch(
        "SELECT a.id FROM google_accounts a JOIN households h ON h.id = a.household_id "
        "WHERE a.disconnected_at IS NULL AND NOT a.needs_reconnect AND h.status = 'active' "
        # A mailbox still on its first pass is read again at once: the backfill
        # is paginated, and waiting five minutes a page would take all day.
        "AND (a.last_polled_at IS NULL OR NOT a.backfill_done OR a.last_polled_at < $1) "
        "ORDER BY a.last_polled_at NULLS FIRST LIMIT $2", now - every, limit)
    return [r["id"] for r in rows]


async def sweep(*, now: dt.datetime | None = None, transport=None) -> dict:
    now = now or dt.datetime.now(dt.UTC)
    totals = {"mailboxes": 0, "stored": 0, "duplicates": 0, "refused": 0, "failed": 0, "stalled": 0}
    for account_id in await due(now=now):
        totals["mailboxes"] += 1
        try:
            result = await poll(account_id, now=now, transport=transport)
        except google.GoogleError as exc:
            # Rate limited or Google is down: leave it for the next sweep
            # rather than failing the whole job.
            logger.warning("polling a mailbox failed: %s", exc)
            async with pool().acquire() as conn:
                await conn.execute("UPDATE google_accounts SET last_error = $2 WHERE id = $1",
                                   account_id, str(exc)[:500])
            continue
        for field in ("stored", "duplicates", "refused", "failed"):
            totals[field] += result.get(field, 0)
        totals["stalled"] += 1 if result.get("stalled") else 0
    return totals


# ----------------------------------------------------------------------------
# the job
# ----------------------------------------------------------------------------

async def _handle(job: dict, ctx: jobs.JobContext) -> dict:
    now = dt.datetime.now(dt.UTC)
    await ctx.plan(["Read the connected mailboxes"])
    await ctx.step("0")
    totals = await sweep(now=now)
    await ctx.finish_step("0")
    every = max(60, settings.gmail_poll_seconds)
    return {**totals, "next_run_at": (now + dt.timedelta(seconds=every)).isoformat()}


async def ensure_scheduled() -> dict | None:
    """One sweep for the whole deployment, like the reminders. Off unless
    Gmail is enabled and there is a client to use."""
    if not (settings.gmail_enabled and configured()):
        return None
    existing = await pool().fetchrow(
        "SELECT id FROM jobs WHERE kind = $1 AND status NOT IN ('succeeded', 'failed', 'cancelled') LIMIT 1", KIND)
    if existing is not None:
        return None
    return await jobs.create(KIND, {}, next_run_at=dt.datetime.now(dt.UTC))


def register() -> None:
    jobs.register(KIND, _handle, version=HANDLER_VERSION,
                  settings_keys=("gmail_query", "gmail_poll_seconds", "gmail_backfill_days",
                                 "gmail_max_per_poll"))


def describe() -> str:
    """Said at boot, next to what reads notices: a connected mailbox is the
    one channel that reads mail nobody chose to send us."""
    if not settings.gmail_enabled:
        return "Gmail intake is off (FAMILYOS_GMAIL_ENABLED=false)."
    if not configured():
        return ("Gmail intake is MISCONFIGURED: FAMILYOS_GMAIL_ENABLED is true but no Google OAuth client is set, "
                "so no mailbox can be connected.")
    return (f"Gmail intake is on, polling every {max(60, settings.gmail_poll_seconds)}s with "
            f"gmail.readonly only; query: {settings.gmail_query!r}")


__all__ = ["KIND", "begin", "complete", "configured", "describe", "disconnect", "due",
           "ensure_scheduled", "list_for", "poll", "register", "sweep", "NotConfigured"]
