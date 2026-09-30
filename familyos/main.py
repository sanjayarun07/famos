"""FastAPI application. Run with: uvicorn familyos.main:app"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from familyos import db, erasure, jobs, reminders
from familyos.api.routes import router
from familyos.artifacts import HouseholdUnavailable
from familyos.consent import ConsentRequired
from familyos.extraction import service as extraction
from familyos.identity import Invalid, NotAllowed, NotFound
from familyos.intake.gateway import Rejected
from familyos.settings import settings

logger = logging.getLogger("familyos")

WEB = Path(__file__).parent / "web"


def register_job_handlers() -> None:
    erasure.register()
    extraction.register()
    reminders.register()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await db.connect()
    await db.migrate()
    register_job_handlers()
    if settings.jobs_enabled:
        await reminders.ensure_scheduled()
    worker = asyncio.create_task(jobs.worker()) if settings.jobs_enabled else None
    try:
        yield
    finally:
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        await db.close()


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    app = FastAPI(title="FamilyOS", version="0.1.0", lifespan=lifespan if with_lifespan else None)
    app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        await db.pool().fetchval("SELECT 1")
        return {"ok": True}

    # The console is served from this app, so it shares the API's origin: the
    # bearer token goes straight on each request and no CORS rule is needed.
    if WEB.is_dir():
        @app.get("/", include_in_schema=False)
        async def root():
            return RedirectResponse("/app/")

        app.mount("/app", StaticFiles(directory=WEB, html=True), name="console")

    def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": code, "detail": message, **extra})

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound):
        return _error(404, "not_found", f"{exc} not found")

    @app.exception_handler(NotAllowed)
    async def not_allowed(_: Request, exc: NotAllowed):
        return _error(403, "not_allowed", str(exc))

    @app.exception_handler(Invalid)
    async def invalid(_: Request, exc: Invalid):
        return _error(422, "invalid", str(exc))

    @app.exception_handler(ConsentRequired)
    async def consent_required(_: Request, exc: ConsentRequired):
        return _error(409, "consent_required", "a guardian must give consent before recording anything about this child",
                      member_ids=[str(m) for m in exc.member_ids])

    @app.exception_handler(Rejected)
    async def rejected(_: Request, exc: Rejected):
        return _error(exc.status, "rejected", exc.reason)

    @app.exception_handler(HouseholdUnavailable)
    async def unavailable(_: Request, exc: HouseholdUnavailable):
        return _error(409, "household_unavailable", "this household is being erased")

    return app


app = create_app()
