"""Audit events: an append-only record of who did what to which record.

Rules: write the event in the same transaction as the change it describes,
and put ids, counts, sizes and hashes in `detail`, never user content
(filenames, subjects, text). The table rejects UPDATE and DELETE; only an
erasure may purge it (see erasure.py).
"""
from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from familyos.db import pool


async def record(
    household_id: uuid.UUID,
    action: str,
    *,
    actor_member_id: uuid.UUID | None = None,
    actor_kind: str = "member",
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
    conn: asyncpg.Connection | None = None,
) -> None:
    sql = ("INSERT INTO audit_events (household_id, actor_kind, actor_member_id, action, target_type, target_id, detail) "
           "VALUES ($1, $2, $3, $4, $5, $6, $7)")
    args = (household_id, actor_kind, actor_member_id, action, target_type, target_id, detail or {})
    if conn is not None:
        await conn.execute(sql, *args)
    else:
        await pool().execute(sql, *args)


async def list_for(household_id: uuid.UUID, limit: int = 100, before_id: int | None = None) -> list[dict]:
    rows = await pool().fetch(
        "SELECT * FROM audit_events WHERE household_id = $1 AND ($2::bigint IS NULL OR id < $2) ORDER BY id DESC LIMIT $3",
        household_id, before_id, limit)
    return [dict(r) for r in rows]
