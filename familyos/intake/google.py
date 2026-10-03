"""Connected Gmail: reading the mailbox a school already writes to.

Forwarding works and will keep working, but it asks the family to notice a
notice and act on it -- which is the thing they came here because they are bad
at. A read-only connection to the mailbox the school already has removes that
step for every notice that arrives by mail.

Scope, and why it is only one
-----------------------------
`gmail.readonly` and nothing else. Not modify, not send, not labels. It is a
**restricted** scope, which means Google requires a CASA Tier 2 security
assessment before an app may ask the public for it. Until that clears, an
OAuth client in testing mode may ask up to 100 named test users, which is what
an alpha is. Nothing in this module differs between the two cases.

`openid` and `userinfo.email` come along so the token response says which
account was connected -- otherwise the member cannot tell which of their
mailboxes this is, and reconnecting a different one would look identical.

What it asks Gmail for
----------------------
A query, not an inbox. `gmail_query` is deliberately narrow: mail that does
not match is mail FamilyOS never sees, which is a privacy property and not
only a cost one. The first pass reaches back `gmail_backfill_days`; after that
each poll asks for what arrived since the last one, with an hour of overlap so
a late delivery or a clock skew cannot drop a message. Overlap is free because
intake already refuses a Gmail id it has stored before.

Everything here is parsing and HTTP. Deciding who a message belongs to, and
whether it is stored at all, is the gateway's job.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"

SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
)

# Asked-for again on every poll, so a message that arrived while the poller was
# down is not missed to a minute of clock skew.
OVERLAP = dt.timedelta(hours=1)


class GoogleError(RuntimeError):
    """Google refused. `retryable` says whether trying later could help."""

    def __init__(self, message: str, *, status: int = 0, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class NeedsReconnect(GoogleError):
    """The refresh token no longer works: the member revoked access, changed
    their password, or Google expired it. Only they can fix it, so the
    connection is marked and the poller stops asking."""


@dataclass
class Token:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scopes: tuple[str, ...]
    email: str | None
    subject: str | None


@dataclass
class Summary:
    """One message, before anything has been fetched."""
    message_id: str
    thread_id: str | None


def pkce() -> tuple[str, str]:
    """(verifier, challenge). The verifier never leaves the server, so a
    callback cannot be replayed by whoever intercepted the code."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def authorize_url(*, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    """`access_type=offline` with `prompt=consent` is what returns a refresh
    token; without it a reconnection yields an access token that dies in an
    hour and the poller can never run unattended."""
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def claims_from_id_token(id_token: str | None) -> dict:
    """The email and stable subject out of the id_token's payload.

    Not verified, and it does not need to be: this token came back on our own
    TLS connection to Google's token endpoint in response to our own request,
    so there is no third party whose word we are taking. It is read for
    labelling, never for authorisation -- authorisation is the member's
    FamilyOS token on the request that started this.
    """
    if not id_token or id_token.count(".") != 2:
        return {}
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        logger.warning("could not read the id_token payload; the account will be labelled by its scopes only")
        return {}


def token_from(body: dict) -> Token:
    claims = claims_from_id_token(body.get("id_token"))
    return Token(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_in=int(body.get("expires_in") or 0),
        scopes=tuple((body.get("scope") or "").split()),
        email=claims.get("email"),
        subject=claims.get("sub"),
    )


def query_for(base: str, *, since: dt.datetime | None, backfill_days: int, now: dt.datetime) -> str:
    """Gmail search accepts epoch seconds after `after:`, so "since the last
    poll" is expressible exactly rather than to the nearest day."""
    start = (since - OVERLAP) if since else (now - dt.timedelta(days=backfill_days))
    clause = f"after:{int(start.timestamp())}"
    base = (base or "").strip()
    # The base query is OR-ed internally, so it is parenthesised before being
    # AND-ed with the date or the date would bind to the last term only.
    return f"({base}) {clause}" if base else clause


def missing_scopes(granted: tuple[str, ...] | list[str]) -> list[str]:
    """What was asked for and not given. Google lets a member untick scopes,
    and a connection without gmail.readonly can never read anything -- better
    to say so at once than to poll an empty mailbox forever."""
    have = set(granted or ())
    return [scope for scope in SCOPES if scope not in have]


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

async def _call(transport, method: str, url: str, *, headers: dict | None = None,
                params: dict | None = None, data: dict | None = None) -> tuple[int, bytes]:
    if transport is not None:
        return await transport(method, url, headers=headers or {}, params=params or {}, data=data or {})
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(method, url, headers=headers or None, params=params or None,
                                        data=data or None)
        return response.status_code, response.content


def _raise_for(status: int, body: bytes, what: str) -> None:
    if 200 <= status < 300:
        return
    detail = body[:300].decode("utf-8", "replace")
    if status in (400, 401) and b"invalid_grant" in body:
        raise NeedsReconnect(f"{what}: Google will not refresh this connection any more", status=status)
    if status in (401, 403):
        raise GoogleError(f"{what}: {status} {detail}", status=status)
    # 429 and 5xx are worth another go; the job's own backoff decides when.
    raise GoogleError(f"{what}: {status} {detail}", status=status,
                      retryable=status == 429 or 500 <= status < 600)


async def _json(transport, method: str, url: str, *, what: str, **kw) -> dict:
    status, body = await _call(transport, method, url, **kw)
    _raise_for(status, body, what)
    return json.loads(body or b"{}")


async def exchange_code(code: str, verifier: str, *, client_id: str, client_secret: str,
                        redirect_uri: str, transport=None) -> Token:
    body = await _json(transport, "POST", TOKEN_URL, what="exchanging the code", data={
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier})
    return token_from(body)


async def refresh(refresh_token: str, *, client_id: str, client_secret: str, transport=None) -> Token:
    body = await _json(transport, "POST", TOKEN_URL, what="refreshing the token", data={
        "refresh_token": refresh_token, "client_id": client_id, "client_secret": client_secret,
        "grant_type": "refresh_token"})
    # A refresh response carries no refresh_token: the one we hold still stands.
    return token_from(body)


async def revoke(token: str, *, transport=None) -> bool:
    """Tell Google the token is finished with. Best effort -- the connection is
    gone from our side either way, and a token we can no longer decrypt is a
    token nobody can use."""
    status, _ = await _call(transport, "POST", REVOKE_URL, data={"token": token})
    return 200 <= status < 300


async def list_messages(access_token: str, *, query: str, limit: int, page_token: str | None = None,
                        transport=None) -> tuple[list[Summary], str | None]:
    params = {"q": query, "maxResults": str(limit)}
    if page_token:
        params["pageToken"] = page_token
    body = await _json(transport, "GET", GMAIL + "/messages", what="listing messages",
                       headers={"Authorization": "Bearer " + access_token}, params=params)
    found = [Summary(message_id=m["id"], thread_id=m.get("threadId")) for m in body.get("messages") or []]
    return found, body.get("nextPageToken")


async def get_raw(access_token: str, message_id: str, *, transport=None) -> bytes:
    """The whole RFC 822 message, so the stored original is the mail itself --
    headers, Authentication-Results and all -- and not our rendering of it."""
    body = await _json(transport, "GET", f"{GMAIL}/messages/{message_id}", what="fetching a message",
                       headers={"Authorization": "Bearer " + access_token}, params={"format": "raw"})
    raw = body.get("raw")
    if not raw:
        raise GoogleError(f"message {message_id} came back with no body", retryable=True)
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


async def profile(access_token: str, *, transport=None) -> dict:
    """Address and history id. The history id is Gmail's own cursor; it is
    stored so a later version can ask "what changed" instead of "what is
    there"."""
    return await _json(transport, "GET", GMAIL + "/profile", what="reading the profile",
                       headers={"Authorization": "Bearer " + access_token})
