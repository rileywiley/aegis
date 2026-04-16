"""Aegis — AI Chief of Staff. FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from aegis.config import get_settings
from aegis.db.engine import async_session_factory, engine
from aegis.db.repositories import reset_stuck_processing

logger = logging.getLogger("aegis")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # ── Startup ──────────────────────────────────────────
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("Aegis starting on %s:%s  tz=%s", settings.aegis_host, settings.aegis_port, settings.aegis_timezone)

    # DB connection test
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Database connection OK")
    except Exception:
        logger.exception("Database connection FAILED — is PostgreSQL running on port 5434?")
        raise

    # Reset stuck processing items (crash recovery)
    async with async_session_factory() as session:
        reset_count = await reset_stuck_processing(session)
        if reset_count:
            logger.info("Reset %d stuck processing items back to pending", reset_count)

    logger.info("Aegis ready")
    yield

    # ── Shutdown ─────────────────────────────────────────
    logger.info("Aegis shutting down")
    await engine.dispose()


app = FastAPI(
    title="Aegis — AI Chief of Staff",
    version="0.1.0",
    lifespan=lifespan,
)

# ── Routers ──────────────────────────────────────────────
from aegis.web.routes.dashboard import router as dashboard_router  # noqa: E402
from aegis.web.routes.meetings import router as meetings_router  # noqa: E402
from aegis.web.routes.stubs import router as stubs_router  # noqa: E402

app.include_router(dashboard_router)
app.include_router(meetings_router)
app.include_router(stubs_router)
