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

    @property
    def is_guardian(self) -> bool:
        return self.role == "guardian"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def inbound_address(inbound_token: str) -> str:
    return f"{inbound_token}@{settings.inbound_domain}"


async def _issue_token(conn: asyncpg.Connection, member_id: uuid.UUID) -> str:
    token = "fos_" + secrets.token_urlsafe(32)
    await conn.execute("INSERT INTO member_tokens (id, member_id, token_hash) VALUES ($1, $2, $3)",
                       uuid.uuid4(), member_id, _hash(token))
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
    member_id = uuid.uuid4()
    async with pool().acquire() as conn, conn.transaction():
        try:
            member = await conn.fetchrow(
                "INSERT INTO members (id, household_id, display_name, role, email, date_of_birth) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
                member_id, actor.household_id, data.display_name, data.role,
                str(data.email).lower() if data.email else None, data.date_of_birth)
        except asyncpg.UniqueViolationError as exc:
            raise Invalid("a member with this email already exists in the household") from exc
        token = await _issue_token(conn, member_id) if data.role != "child" else None
        await audit.record(actor.household_id, "member.added", actor_member_id=actor.member_id, target_type="member",
                           target_id=member_id, detail={"role": data.role}, conn=conn)
    return dict(member), token


async def authenticate(token: str) -> Principal | None:
    row = await pool().fetchrow(
        "SELECT m.id, m.household_id, m.role, m.display_name FROM member_tokens t "
        "JOIN members m ON m.id = t.member_id JOIN households h ON h.id = m.household_id "
        "WHERE t.token_hash = $1 AND t.revoked_at IS NULL AND h.status = 'active' AND m.role <> 'child'",
        _hash(token))
    if row is None:
        return None
    return Principal(member_id=row["id"], household_id=row["household_id"], role=row["role"], display_name=row["display_name"])


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


async def member_by_email(household_id: uuid.UUID, email: str) -> dict | None:
    row = await pool().fetchrow(
        "SELECT * FROM members WHERE household_id = $1 AND lower(email) = lower($2) AND role <> 'child'", household_id, email)
    return dict(row) if row else None
