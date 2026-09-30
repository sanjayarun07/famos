"""Households, members and sign-in tokens.

A household is the unit of tenancy, encryption and erasure. Members are
guardians (can consent for children and manage the household), adults
(sign in, see what is shared) and children (data subjects who do not sign
in in v1). Following Orbit's identity split, the person acting and the
household that owns the data are separate: every read and write names both.

Sign-in in v1 is a bearer token issued when a member is created. It stands
in for real authentication (phone OTP or email magic link), which replaces
`_issue_token` without changing anything that consumes a Principal.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass

import asyncpg

from familyos import audit, crypto
from familyos.db import pool
from familyos.intake.whatsapp import normalise_phone
from familyos.models import HouseholdIn, MemberIn
from familyos.settings import settings


class NotAllowed(Exception):
    """The acting member may not do this."""


class NotFound(Exception):
    pass


class Invalid(ValueError):
    """The request is well-formed but not acceptable."""


@dataclass(frozen=True)
class Principal:
    member_id: uuid.UUID
    household_id: uuid.UUID
    role: str
    display_name: str
    # Which token this request arrived on, so signing out can end that one and
    # leave the member's other devices alone.
    token_id: uuid.UUID | None = None

    @property
    def is_guardian(self) -> bool:
        return self.role == "guardian"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def inbound_address(inbound_token: str) -> str:
    return f"{inbound_token}@{settings.inbound_domain}"


async def _issue_token(conn: asyncpg.Connection, member_id: uuid.UUID) -> str:
    token = "fos_" + secrets.token_urlsafe(32)
    await conn.execute(
        "INSERT INTO member_tokens (id, member_id, token_hash, expires_at) "
        "VALUES ($1, $2, $3, NOW() + make_interval(days => $4))",
        uuid.uuid4(), member_id, _hash(token), settings.token_lifetime_days)
    return token


async def create_household(data: HouseholdIn) -> tuple[dict, dict, str]:
    """A new household with its first guardian. Returns (household, member, token)."""
    household_id, member_id = uuid.uuid4(), uuid.uuid4()
    async with pool().acquire() as conn, conn.transaction():
        household = await conn.fetchrow(
            "INSERT INTO households (id, name, inbound_token, wrapped_key) VALUES ($1, $2, $3, $4) RETURNING *",
            household_id, data.name, "h" + secrets.token_hex(10), crypto.new_wrapped_key(household_id))
        member = await conn.fetchrow(
            "INSERT INTO members (id, household_id, display_name, role, email) VALUES ($1, $2, $3, 'guardian', $4) RETURNING *",
            member_id, household_id, data.guardian.display_name, str(data.guardian.email).lower())
        token = await _issue_token(conn, member_id)
        await audit.record(household_id, "household.created", actor_member_id=member_id,
                           target_type="household", target_id=household_id, conn=conn)
        await audit.record(household_id, "member.added", actor_member_id=member_id, target_type="member",
                           target_id=member_id, detail={"role": "guardian"}, conn=conn)
    return dict(household), dict(member), token


async def add_member(actor: Principal, data: MemberIn) -> tuple[dict, str | None]:
    """Guardians add members. Children get no token: they do not sign in."""
    if not actor.is_guardian:
        raise NotAllowed("only a guardian can add members")
    if data.role == "child" and data.email:
        raise Invalid("a child member has no email address in v1")
    if data.role != "child" and not data.email:
        raise Invalid("a guardian or adult member needs an email address")
    phone = normalise_phone(data.phone) if data.role != "child" else None
    if data.role == "child" and data.phone:
        raise Invalid("a child member has no phone number in v1")
    member_id = uuid.uuid4()
    async with pool().acquire() as conn, conn.transaction():
        try:
            member = await conn.fetchrow(
                "INSERT INTO members (id, household_id, display_name, role, email, phone, date_of_birth) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
                member_id, actor.household_id, data.display_name, data.role,
                str(data.email).lower() if data.email else None, phone, data.date_of_birth)
        except asyncpg.UniqueViolationError as exc:
            raise Invalid("a member with this email or phone number already exists in the household") from exc
        token = await _issue_token(conn, member_id) if data.role != "child" else None
        await audit.record(actor.household_id, "member.added", actor_member_id=actor.member_id, target_type="member",
                           target_id=member_id, detail={"role": data.role}, conn=conn)
    return dict(member), token


async def authenticate(token: str, *, allow_erasing: bool = False) -> Principal | None:
    """A signed-in member. An erasing household signs nobody in; the one
    exception is reading the erasure's own progress (`allow_erasing`), which
    would otherwise be unreachable because erasure invalidates every token
    the moment it starts."""
    digest = _hash(token)
    row = await pool().fetchrow(
        "SELECT t.id AS token_id, m.id, m.household_id, m.role, m.display_name FROM member_tokens t "
        "JOIN members m ON m.id = t.member_id JOIN households h ON h.id = m.household_id "
        "WHERE t.token_hash = $1 AND t.revoked_at IS NULL AND m.role <> 'child' "
        "AND (t.expires_at IS NULL OR t.expires_at > NOW()) "
        "AND (h.status = 'active' OR ($2 AND h.status = 'erasing'))",
        digest, allow_erasing)
    if row is None:
        return None
    # Slide the expiry forward while the token is in use, at most once an hour
    # so a busy session is not a write per request. A token nobody uses ends.
    await pool().execute(
        "UPDATE member_tokens SET last_used_at = NOW(), expires_at = NOW() + make_interval(days => $2) "
        "WHERE id = $1 AND (last_used_at IS NULL OR last_used_at < NOW() - INTERVAL '1 hour')",
        row["token_id"], settings.token_lifetime_days)
    return Principal(member_id=row["id"], household_id=row["household_id"], role=row["role"],
                     display_name=row["display_name"], token_id=row["token_id"])


async def sign_out(actor: Principal) -> int:
    """End the token this request arrived on, and nothing else: signing out on
    the shared laptop must not sign you out on your phone."""
    if actor.token_id is None:
        return 0
    async with pool().acquire() as conn, conn.transaction():
        done = await conn.execute(
            "UPDATE member_tokens SET revoked_at = NOW(), revoked_reason = 'signed_out' "
            "WHERE id = $1 AND revoked_at IS NULL", actor.token_id)
        ended = 0 if done == "UPDATE 0" else 1
        if ended:
            await audit.record(actor.household_id, "session.signed_out", actor_member_id=actor.member_id,
                               target_type="member", target_id=actor.member_id, conn=conn)
    return ended


async def revoke_all(actor: Principal, member_id: uuid.UUID) -> int:
    """Every token a member holds, ended at once. Yourself, or -- when a phone
    is lost and its owner cannot reach it -- anyone in the household, by a
    guardian. Children have no tokens to end."""
    if member_id != actor.member_id and not actor.is_guardian:
        raise NotAllowed("only a guardian can sign another member out")
    async with pool().acquire() as conn, conn.transaction():
        member = await conn.fetchrow("SELECT id FROM members WHERE id = $1 AND household_id = $2",
                                     member_id, actor.household_id)
        if member is None:
            raise NotFound("member")
        rows = await conn.fetch(
            "UPDATE member_tokens SET revoked_at = NOW(), revoked_reason = 'revoked' "
            "WHERE member_id = $1 AND revoked_at IS NULL RETURNING id", member_id)
        if rows:
            await audit.record(actor.household_id, "session.revoked", actor_member_id=actor.member_id,
                               target_type="member", target_id=member_id,
                               detail={"tokens_ended": len(rows)}, conn=conn)
    return len(rows)


async def sessions(actor: Principal, member_id: uuid.UUID | None = None) -> list[dict]:
    """The live tokens for a member: yours, or anyone's if you are a guardian.
    The token itself is never shown again -- only when it started, when it was
    last used, and when it ends."""
    member_id = member_id or actor.member_id
    if member_id != actor.member_id and not actor.is_guardian:
        raise NotAllowed("only a guardian can see another member's sessions")
    rows = await pool().fetch(
        "SELECT t.id, t.member_id, t.created_at, t.last_used_at, t.expires_at "
        "FROM member_tokens t JOIN members m ON m.id = t.member_id "
        "WHERE t.member_id = $1 AND m.household_id = $2 AND t.revoked_at IS NULL "
        "AND (t.expires_at IS NULL OR t.expires_at > NOW()) ORDER BY t.created_at DESC",
        member_id, actor.household_id)
    return [{**dict(r), "current": r["id"] == actor.token_id} for r in rows]


async def get_household(household_id: uuid.UUID) -> dict:
    household = await pool().fetchrow("SELECT * FROM households WHERE id = $1", household_id)
    if household is None:
        raise NotFound("household")
    members = await pool().fetch("SELECT * FROM members WHERE household_id = $1 ORDER BY created_at", household_id)
    return {**dict(household), "members": [dict(m) for m in members]}


async def get_member(household_id: uuid.UUID, member_id: uuid.UUID) -> dict:
    row = await pool().fetchrow("SELECT * FROM members WHERE id = $1 AND household_id = $2", member_id, household_id)
    if row is None:
        raise NotFound("member")
    return dict(row)


async def household_by_inbound_token(inbound_token: str) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM households WHERE inbound_token = $1 AND status = 'active'", inbound_token)
    return dict(row) if row else None


async def member_by_phone(number: str) -> dict | None:
    """The member a WhatsApp number belongs to, and so the household a
    forwarded message lands in. There is no per-household WhatsApp address the
    way there is for email -- every message arrives at one business number --
    so the sender is the only thing that can place it.

    A number in two households is ambiguous and places nothing: guessing which
    family a school notice belongs to is not a guess worth making."""
    from familyos.intake.whatsapp import phone_variants

    rows = await pool().fetch(
        "SELECT m.* FROM members m JOIN households h ON h.id = m.household_id "
        "WHERE m.phone = ANY($1::text[]) AND m.role <> 'child' AND h.status = 'active'",
        phone_variants(number))
    if len(rows) != 1:
        return None
    return dict(rows[0])


async def member_by_email(household_id: uuid.UUID, email: str) -> dict | None:
    row = await pool().fetchrow(
        "SELECT * FROM members WHERE household_id = $1 AND lower(email) = lower($2) AND role <> 'child'", household_id, email)
    return dict(row) if row else None
