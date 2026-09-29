"""Durable jobs: work that outlives one request.

Copied from Orbit (github.com/sanjayarun07/orbit, app/jobs.py @ 914c3d9)
and adapted; docs/provenance.md lists every change. Keep the structure
close to the original so later Orbit fixes can be ported by diff.

A job is a row that carries its own plan, working state, evidence, typed
signals and an idempotent operation cache. One worker per process claims
due jobs with a compare-and-swap on (id, status, lease_id), heartbeats the
lease, and stops at the next guard when the lease is gone. Every write to a
running job is that same compare-and-swap: the row is the lock, so two
processes need no other coordination. A lost lease is not a failure -- the
job goes back to `queued` with everything intact and the next claim resumes
from the last checkpoint. Cached operations are returned without a call,
so a resume never repeats a paid call.

States: queued -> running -> succeeded | failed | cancelled, with
waiting_input / waiting_approval as pauses and scheduled for recurring
work. Handlers are registered per kind and must be restartable from any
checkpoint: read the plan, skip finished steps.

A job belongs to a household (and cascades with it) or, for system work
such as erasing a household, to no household at all.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from familyos.db import pool
from familyos.settings import settings

logger = logging.getLogger(__name__)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
PAUSED = frozenset({"waiting_input", "waiting_approval"})
MAX_PLAN_STEPS = 12
MAX_ACTIVE = 3
TICK_SECONDS = 1.0
VOLATILE_MAX_AGE_SECONDS = 300.0
_JSON = ("spec", "plan", "state", "evidence", "operations", "signals", "result")
_UUID = ("id", "household_id", "member_id")

_handlers: dict[str, Callable[[dict, JobContext], Awaitable[dict]]] = {}
_assumptions: dict[str, tuple[str, tuple[str, ...]]] = {}     # kind -> (handler version, settings the cache depends on)
_active: dict[str, asyncio.Task] = {}
_worker_running = False
_settled: dict[str, asyncio.Event] = {}      # attach() waits on these in-process


class LostLease(RuntimeError):
    """The lease is gone: paused, cancelled, taken over, or the household erased."""


def register(kind: str, handler: Callable[[dict, JobContext], Awaitable[dict]], *, version: str = "1",
             settings_keys: tuple[str, ...] = ()) -> None:
    """A handler registers with the assumptions its cached evidence depends
    on: its own version and the settings that shape what it fetches. They
    make up the job's fingerprint (see `fingerprint`)."""
    _handlers[kind] = handler
    _assumptions[kind] = (version, tuple(settings_keys))


def fingerprint(kind: str, spec: dict) -> str:
    """What a job's checkpoints were built under: kind, spec, handler
    version and the named settings. A resumed job whose fingerprint no
    longer matches the running code drops its cache and restarts its plan,
    so evidence gathered under one set of assumptions is never mixed with
    another."""
    version, keys = _assumptions.get(kind, ("1", ()))
    basis = {"kind": kind, "spec": spec, "version": version, "settings": {k: getattr(settings, k, None) for k in keys}}
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:32]


def reset_for_test() -> None:
    global _worker_running
    _active.clear()
    _settled.clear()
    _worker_running = False


def _now() -> datetime:
    return datetime.now(UTC)


def _from_db(r) -> dict:
    row = dict(r)
    for key in _UUID:
        if row.get(key) is not None:
            row[key] = str(row[key])
    for key in _JSON:
        value = row.get(key)
        if isinstance(value, str):
            row[key] = json.loads(value)
    return row


# ----------------------------------------------------------------------------
# store: create, read, compare-and-swap
# ----------------------------------------------------------------------------

async def create(kind: str, spec: dict, *, household_id: uuid.UUID | str | None = None,
                 member_id: uuid.UUID | str | None = None, next_run_at: datetime | None = None, conn=None) -> dict:
    """Queue a job. Pass `conn` to create it inside the caller's transaction,
    so the job exists exactly when the change that asked for it does."""
    if kind not in _handlers:
        raise ValueError(f"no handler registered for job kind {kind!r}")
    job_id = uuid.uuid4()
    spec = dict(spec or {})
    db = conn or pool()
    r = await db.fetchrow(
        "INSERT INTO jobs (id, household_id, member_id, kind, status, spec, next_run_at, fingerprint) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
        job_id, _uuid(household_id), _uuid(member_id), kind, "scheduled" if next_run_at else "queued", spec,
        next_run_at, fingerprint(kind, spec))
    await _event(str(job_id), "created", f"{kind} queued", conn=db)
    return _from_db(r)


def _uuid(value) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value else None


async def get(job_id: str) -> dict | None:
    r = await pool().fetchrow("SELECT * FROM jobs WHERE id = $1", uuid.UUID(job_id))
    return _from_db(r) if r else None


async def list_for(household_id: uuid.UUID, limit: int = 50) -> list[dict]:
    rows = await pool().fetch("SELECT * FROM jobs WHERE household_id = $1 ORDER BY created_at DESC LIMIT $2",
                              household_id, limit)
    return [_from_db(r) for r in rows]


async def events(job_id: str) -> list[dict]:
    rows = await pool().fetch("SELECT id, at, kind, title, detail FROM job_events WHERE job_id = $1 ORDER BY at", uuid.UUID(job_id))
    return [{"id": str(r["id"]), "at": r["at"].isoformat(), "kind": r["kind"], "title": r["title"], "detail": r["detail"]} for r in rows]


async def _event(job_id: str, kind: str, title: str, detail: str | None = None, conn=None) -> None:
    try:
        await (conn or pool()).execute("INSERT INTO job_events (id, job_id, kind, title, detail) VALUES ($1, $2, $3, $4, $5)",
                                       uuid.uuid4(), uuid.UUID(job_id), kind, title[:200], (detail or "")[:4000] or None)
    except Exception:   # the job row is gone (its household was erased): nothing to log against
        if conn is not None:
            raise
        logger.debug("job %s: event %s not recorded", job_id, kind, exc_info=True)


_MERGE = ("operations",)                 # dict columns merged key by key
_APPEND = ("evidence", "signals")          # list columns appended to


async def cas(job_id: str, expected: dict, patch: dict, merge: dict | None = None, append: dict | None = None) -> dict | None:
    """Apply `patch` only if every `expected` column still holds; the fresh row,
    or None when someone else got there first. `merge` adds keys into a dict
    column and `append` adds items to a list column atomically in the
    database (jsonb ||), so concurrent checkpoints never overwrite each
    other's entries."""
    patch = {**patch, "updated_at": _now()}
    merge, append = dict(merge or {}), dict(append or {})
    sets, args, n = [], [], 1
    for key, value in patch.items():
        if key in _JSON:
            sets.append(f"{key} = ${n}::jsonb"); args.append(_jsonable(value) if value is not None else None)
        else:
            sets.append(f"{key} = ${n}"); args.append(value)
        n += 1
    for key, value in merge.items():
        sets.append(f"{key} = COALESCE({key}, '{{}}'::jsonb) || ${n}::jsonb"); args.append(_jsonable(value)); n += 1
    for key, value in append.items():
        sets.append(f"{key} = COALESCE({key}, '[]'::jsonb) || ${n}::jsonb"); args.append(_jsonable(list(value))); n += 1
    wheres = [f"id = ${n}"]; args.append(uuid.UUID(job_id)); n += 1
    for key, value in expected.items():
        wheres.append(f"{key} IS NOT DISTINCT FROM ${n}"); args.append(value); n += 1
    r = await pool().fetchrow(f"UPDATE jobs SET {', '.join(sets)} WHERE {' AND '.join(wheres)} RETURNING *", *args)
    return _from_db(r) if r else None


# ----------------------------------------------------------------------------
# the handler's context
# ----------------------------------------------------------------------------

class JobContext:
    def __init__(self, job: dict, lease_id: str):
        self.job = job
        self.lease_id = lease_id
        self.signal = asyncio.Event()

    @property
    def id(self) -> str:
        return self.job["id"]

    async def guard(self) -> None:
        """Raise LostLease unless this lease still owns a running job. A job
        whose household was erased is gone with it, so this also stops it."""
        if self.signal.is_set():
            raise LostLease("cancelled")
        latest = await get(self.id)
        if latest is None or latest.get("lease_id") != self.lease_id or latest.get("status") != "running":
            self.signal.set()
            raise LostLease("lease lost")
        self.job = latest

    async def checkpoint(self, merge: dict | None = None, append: dict | None = None, **patch) -> dict:
        if self.signal.is_set():
            raise LostLease("cancelled")
        fresh = await cas(self.id, {"lease_id": self.lease_id, "status": "running"}, patch, merge=merge, append=append)
        if fresh is None:
            self.signal.set()
            raise LostLease("lease lost at checkpoint")
        self.job = fresh
        return fresh

    async def event(self, kind: str, title: str, detail: str | None = None) -> None:
        await self.guard()
        await _event(self.id, kind, title, detail)

    async def plan(self, steps: list[str]) -> list[dict]:
        """Set the plan once; a resumed job keeps the plan it has."""
        if self.job.get("plan"):
            return self.job["plan"]
        plan = [{"id": str(i), "title": t, "status": "pending", "started_at": None, "finished_at": None, "note": None}
                for i, t in enumerate(steps[:MAX_PLAN_STEPS])]
        await self.checkpoint(plan=plan)
        return plan

    def done(self, step_id: str) -> bool:
        return any(s["id"] == step_id and s["status"] == "done" for s in self.job.get("plan") or [])

    async def step(self, step_id: str, title: str | None = None) -> None:
        plan = [dict(s) for s in self.job.get("plan") or []]
        for s in plan:
            if s["id"] == step_id and s["status"] != "done":
                s["status"], s["started_at"] = "running", _now().isoformat()
        await self.checkpoint(plan=plan)
        await _event(self.id, "step", title or next((s["title"] for s in plan if s["id"] == step_id), step_id))

    async def finish_step(self, step_id: str, note: str | None = None) -> None:
        plan = [dict(s) for s in self.job.get("plan") or []]
        for s in plan:
            if s["id"] == step_id:
                s["status"], s["finished_at"], s["note"] = "done", _now().isoformat(), note
        await self.checkpoint(plan=plan)

    async def call(self, name: str, factory: Callable[[], Awaitable[Any] | Any], *, args: Any = None, volatile: bool = False,
                   cost_usd: float = 0.0) -> Any:
        """The operation cache. A cached result comes back without a call and
        writes no event; a fresh one is made, cached with its cost at once, and
        checkpointed. Volatile results are refetched past
        VOLATILE_MAX_AGE_SECONDS; everything else lives with the job."""
        key = hashlib.sha256(f"{name}:{json.dumps(args, sort_keys=True, default=str)}".encode()).hexdigest()
        cached = (self.job.get("operations") or {}).get(key)
        if cached is not None:
            age = (_now() - datetime.fromisoformat(cached["at"])).total_seconds()
            if not volatile or age <= VOLATILE_MAX_AGE_SECONDS:
                return cached["result"]
        await self.guard()
        result = factory()
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            result = await result
        entry = {"name": name, "result": _jsonable(result), "at": _now().isoformat(), "cost_usd": cost_usd, "volatile": volatile}
        await self.checkpoint(merge={"operations": {key: entry}})       # one key, merged; concurrent calls keep each other's
        return entry["result"]

    async def add_evidence(self, item: dict) -> None:
        await self.checkpoint(append={"evidence": [item]})

    async def add_signal(self, signal: Any) -> None:
        dumped = signal.model_dump(mode="json") if hasattr(signal, "model_dump") else signal
        await self.checkpoint(append={"signals": [_jsonable(dumped)]})

    async def ask(self, question: str) -> None:
        """Pause for a member: the question goes to them, their answer
        requeues the job."""
        await self.checkpoint(status="waiting_input", question=question, lease_id=None, lease_until=None)
        await _event(self.id, "paused", "Waiting for an answer", question)
        self.signal.set()
        raise _Paused()

    async def approve(self, action_id: str) -> None:
        """Pause for a member's approval of the named action."""
        await self.checkpoint(status="waiting_approval", action_id=action_id, lease_id=None, lease_until=None)
        await _event(self.id, "paused", "Waiting for approval", action_id)
        self.signal.set()
        raise _Paused()


class _Paused(Exception):
    """Raised inside a handler by ask()/approve() to unwind it cleanly."""


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=_default))
    except (TypeError, ValueError):
        return str(value)


def _default(value: Any):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        import dataclasses
        return dataclasses.asdict(value)
    return str(value)


# ----------------------------------------------------------------------------
# claiming and running
# ----------------------------------------------------------------------------

async def due(now: datetime | None = None, limit: int = MAX_ACTIVE) -> list[dict]:
    now = now or _now()
    rows = await pool().fetch(
        "SELECT * FROM jobs WHERE status = 'queued' OR (status = 'scheduled' AND next_run_at <= $1) "
        "OR (status = 'running' AND lease_until < $1) ORDER BY created_at LIMIT $2", now, limit)
    return [_from_db(r) for r in rows]


async def claim(row: dict, lease_id: str | None = None) -> tuple[dict, str] | None:
    """Take the lease by compare-and-swap on the row as last seen. None when
    another worker got it, or it moved on."""
    lease_id = lease_id or uuid.uuid4().hex
    expected = {"status": row["status"], "lease_id": row.get("lease_id")}
    if row["status"] == "running":
        expected["lease_until"] = row.get("lease_until")
    fresh = await cas(row["id"], expected, {"status": "running", "lease_id": lease_id,
                                           "lease_until": _now() + timedelta(seconds=settings.job_lease_seconds),
                                           "attempts": int(row.get("attempts") or 0) + 1})
    return (fresh, lease_id) if fresh else None


async def run(row: dict) -> dict | None:
    """Claim and run one job to its next pause or settlement. Returns the
    row as left, or None when the claim failed."""
    claimed = await claim(row)
    if claimed is None:
        return None
    job, lease_id = claimed
    current = fingerprint(job["kind"], job.get("spec") or {})
    if job.get("fingerprint") != current:
        # The assumptions changed since the checkpoints were written (a
        # deploy, a setting): nothing cached may be reused.
        job = await cas(job["id"], {"lease_id": lease_id, "status": "running"},
                        {"plan": [], "state": {}, "operations": {}, "evidence": [], "signals": [], "fingerprint": current}) or job
        await _event(job["id"], "reset", "Assumptions changed; the plan restarts without its cache",
                     f"{(row.get('fingerprint') or 'none')[:12]} -> {current[:12]}")
    ctx = JobContext(job, lease_id)
    handler = _handlers.get(job["kind"])
    if handler is None:
        return await _settle(ctx, "failed", error=f"no handler for kind {job['kind']!r}")
    await _event(job["id"], "resumed" if job["attempts"] > 1 else "started", f"attempt {job['attempts']}")
    heartbeat = asyncio.create_task(_heartbeat(ctx))
    try:
        result = await handler(job, ctx)
        return await _settle(ctx, "succeeded", result=result)
    except _Paused:
        return await get(job["id"])
    except LostLease:
        # Not a failure: back to the queue with state intact. A job whose
        # household was erased no longer exists, and the update is a no-op.
        await cas(job["id"], {"lease_id": lease_id, "status": "running"}, {"status": "queued", "lease_id": None, "lease_until": None})
        return await get(job["id"])
    except asyncio.CancelledError:
        await cas(job["id"], {"lease_id": lease_id, "status": "running"}, {"status": "queued", "lease_id": None, "lease_until": None})
        raise
    except Exception as exc:  # noqa: BLE001 - a handler error is the job's to record
        logger.warning("job %s (%s) failed", job["id"], job["kind"], exc_info=True)
        await _event(job["id"], "error", "Attempt failed", f"{type(exc).__name__}: {exc}"[:2000])
        exhausted = job["attempts"] >= settings.job_max_attempts
        if exhausted:
            return await _settle(ctx, "failed", error=f"{type(exc).__name__}: {exc}"[:2000])
        await cas(job["id"], {"lease_id": lease_id, "status": "running"}, {"status": "queued", "lease_id": None, "lease_until": None,
                                                                          "error": f"{type(exc).__name__}: {exc}"[:2000]})
        return await get(job["id"])
    finally:
        heartbeat.cancel()
        _active.pop(job["id"], None)


async def _heartbeat(ctx: JobContext) -> None:
    period = max(1.0, settings.job_lease_seconds / 3)
    try:
        while not ctx.signal.is_set():
            await asyncio.sleep(period)
            fresh = await cas(ctx.id, {"lease_id": ctx.lease_id, "status": "running"},
                              {"lease_until": _now() + timedelta(seconds=settings.job_lease_seconds)})
            if fresh is None:
                ctx.signal.set()
                return
    except asyncio.CancelledError:
        return


async def _settle(ctx: JobContext, status: str, *, result: dict | None = None, error: str | None = None) -> dict | None:
    patch: dict = {"status": status, "lease_id": None, "lease_until": None, "settled_at": _now(), "error": error}
    if result is not None:
        patch["result"] = _jsonable(result)
        # A recurring handler returns next_run_at: the row goes back to
        # `scheduled` with its plan and cache intact, one row for every run.
        next_at = result.get("next_run_at") if isinstance(result, dict) else None
        if status == "succeeded" and next_at:
            patch.update({"status": "scheduled", "next_run_at": datetime.fromisoformat(next_at) if isinstance(next_at, str) else next_at,
                          "settled_at": None, "plan": [], "attempts": 0})
            status = "scheduled"
    fresh = await cas(ctx.id, {"lease_id": ctx.lease_id, "status": "running"}, patch)
    if fresh is None:
        return await get(ctx.id)
    await _event(ctx.id, "settled" if status != "scheduled" else "rescheduled", status, error)
    _notify_settled(ctx.id)
    return fresh


def _notify_settled(job_id: str) -> None:
    ev = _settled.get(job_id)
    if ev is not None:
        ev.set()


# ----------------------------------------------------------------------------
# waiting on, answering, approving, cancelling
# ----------------------------------------------------------------------------

async def attach(job_id: str, timeout: float | None = None) -> dict:
    """Wait for the job to settle or pause for up to `timeout` seconds.
    Without a worker in this process (tests, a bare dev server) the job
    runs inline. Returns the row as it stands."""
    timeout = settings.job_attach_seconds if timeout is None else timeout
    ev = _settled.setdefault(job_id, asyncio.Event())
    try:
        if not _worker_running:
            row = await get(job_id)
            if row and row["status"] in ("queued", "scheduled"):
                await run(row)
            return await get(job_id) or {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = await get(job_id)
            if row is None or row["status"] in TERMINAL or row["status"] in PAUSED:
                return row or {}
            try:
                await asyncio.wait_for(ev.wait(), timeout=min(1.0, max(0.05, deadline - time.monotonic())))
            except TimeoutError:
                pass
        return await get(job_id) or {}
    finally:
        _settled.pop(job_id, None)


async def answer(job_id: str, member_id: str, text: str) -> dict | None:
    """A member's reply to a waiting_input question requeues the job with
    the answer in its state."""
    row = await get(job_id)
    if row is None or row.get("member_id") != str(member_id) or row["status"] != "waiting_input":
        return None
    fresh = await cas(job_id, {"status": "waiting_input"}, {"status": "queued", "question": None,
                                                            "state": {**(row.get("state") or {}), "answer": text}})
    if fresh:
        await _event(job_id, "resumed", "Answer received", text[:500])
    return fresh


async def approved(job_id: str, action_id: str) -> dict | None:
    row = await get(job_id)
    if row is None or row["status"] != "waiting_approval" or row.get("action_id") != action_id:
        return None
    fresh = await cas(job_id, {"status": "waiting_approval"}, {"status": "queued", "action_id": None,
                                                               "state": {**(row.get("state") or {}), "approved": action_id}})
    if fresh:
        await _event(job_id, "resumed", "Approved", action_id)
    return fresh


async def expire_approval(job_id: str, action_id: str) -> dict | None:
    """The approval expired unconfirmed: the step failed, the job is
    requeued to ask again; nothing was done twice."""
    row = await get(job_id)
    if row is None or row["status"] != "waiting_approval" or row.get("action_id") != action_id:
        return None
    fresh = await cas(job_id, {"status": "waiting_approval"}, {"status": "queued", "action_id": None,
                                                               "state": {**(row.get("state") or {}), "expired": action_id}})
    if fresh:
        await _event(job_id, "error", "Approval expired", action_id)
    return fresh


async def cancel(job_id: str, household_id: str) -> dict | None:
    row = await get(job_id)
    if row is None or row.get("household_id") != str(household_id) or row["status"] in TERMINAL:
        return None
    fresh = await cas(job_id, {"status": row["status"]}, {"status": "cancelled", "lease_id": None, "lease_until": None, "settled_at": _now()})
    if fresh:
        await _event(job_id, "settled", "cancelled")
        task = _active.get(job_id)
        if task is not None:
            task.cancel()
        _notify_settled(job_id)
    return fresh


# ----------------------------------------------------------------------------
# the worker
# ----------------------------------------------------------------------------

async def tick(now: datetime | None = None) -> int:
    started = 0
    for row in await due(now):
        if row["id"] in _active or len(_active) >= MAX_ACTIVE:
            continue
        _active[row["id"]] = asyncio.create_task(run(row))
        started += 1
    return started


async def worker() -> None:
    """One per process; the row-level lease makes processes safe together."""
    global _worker_running
    if not settings.jobs_enabled:
        return
    _worker_running = True
    try:
        while True:
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("jobs worker tick failed", exc_info=True)
            await asyncio.sleep(TICK_SECONDS)
    finally:
        _worker_running = False


def public(row: dict) -> dict:
    keep = ("id", "kind", "status", "plan", "attempts", "question", "action_id", "result", "error",
            "created_at", "updated_at", "settled_at")
    return {k: row.get(k) for k in keep}
