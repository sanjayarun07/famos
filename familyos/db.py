"""The Postgres pool and the migration runner.

FamilyOS has one store of record: Postgres. There is no in-memory fallback;
if the database is down, requests fail rather than silently losing writes.
Migrations are plain SQL files in familyos/migrations, applied once each in
name order under an advisory lock, so several processes can start together.
"""
from __future__ import annotations

import json
from pathlib import Path

import asyncpg

from familyos.settings import settings

MIGRATIONS = Path(__file__).parent / "migrations"
_LOCK_KEY = 0x46414D4F  # "FAMO"

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def connect(dsn: str | None = None) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn or settings.database_url, min_size=1, max_size=10, init=_init_connection)
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialised; call db.connect() first")
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def migrate(p: asyncpg.Pool | None = None) -> list[str]:
    """Apply pending migrations; returns the versions applied now."""
    p = p or pool()
    applied_now: list[str] = []
    async with p.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.stem in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.stem)
                applied_now.append(path.stem)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
    return applied_now
