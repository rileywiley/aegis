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


async def _run_processing_cycle() -> None:
    """30-minute processing cycle: triage → extraction → workstream assignment.

    Must run sequentially — each step depends on the previous one's output.
    """
    from aegis.processing.triage import triage_batch, apply_triage_results
    from aegis.processing.pipeline import process_pending_meetings
    from aegis.processing.workstream_detector import run_workstream_assignment

    try:
        # Step 1: Triage new emails + chat messages
        async with async_session_factory() as session:
            from aegis.db.models import Email, ChatMessage
            from sqlalchemy import select

            # Gather untriaged human emails
            stmt = select(Email).where(
                Email.email_class == "human",
                Email.triage_class.is_(None),
                Email.processing_status == "pending",
            )
            result = await session.execute(stmt)
            emails = list(result.scalars().all())
            if emails:
                items = [{"id": e.id, "preview": (e.body_preview or e.subject or "")[:500], "source_type": "email"} for e in emails]
                results = await triage_batch(session, items)
                if results:
                    await apply_triage_results(session, results, "email")
                logger.info("Triage: classified %d emails", len(results))

            # Gather untriaged chat messages
            stmt = select(ChatMessage).where(
                ChatMessage.noise_filtered.is_(False),
                ChatMessage.triage_class.is_(None),
                ChatMessage.processing_status == "pending",
            )
            result = await session.execute(stmt)
            chats = list(result.scalars().all())
            if chats:
                items = [{"id": c.id, "preview": (c.body_preview or c.body_text or "")[:500], "source_type": "chat_message"} for c in chats]
                results = await triage_batch(session, items)
                if results:
                    await apply_triage_results(session, results, "chat_message")
                logger.info("Triage: classified %d chat messages", len(results))

        # Step 2: Run extraction pipeline on pending meetings
        count = await process_pending_meetings()
        if count:
            logger.info("Extraction: processed %d meetings", count)

        # Step 2b: Extract substantive emails
        from aegis.processing.email_extractor import extract_email, store_email_extraction
        from aegis.processing.resolver import resolve_extracted_entities

        async with async_session_factory() as session:
            from sqlalchemy import select, update
            from aegis.db.models import Email

            stmt = select(Email).where(
                Email.triage_class == "substantive",
                Email.processing_status == "pending",
            ).limit(20)  # Process up to 20 per cycle to limit cost
            result = await session.execute(stmt)
            substantive_emails = list(result.scalars().all())

            for email in substantive_emails:
                try:
                    await session.execute(
                        update(Email).where(Email.id == email.id)
                        .values(processing_status="processing")
                    )
                    await session.commit()

                    extraction = await extract_email(session, email.id)
                    if extraction:
                        await resolve_extracted_entities(session, 0, extraction)
                        await store_email_extraction(session, email.id, extraction)

                    await session.execute(
                        update(Email).where(Email.id == email.id)
                        .values(processing_status="completed")
                    )
                    await session.commit()
                except Exception:
                    logger.exception("Email extraction failed for email %d", email.id)
                    await session.execute(
                        update(Email).where(Email.id == email.id)
                        .values(processing_status="failed")
                    )
                    await session.commit()

            if substantive_emails:
                logger.info("Extraction: processed %d substantive emails", len(substantive_emails))

        # Step 3: Workstream assignment
        async with async_session_factory() as session:
            stats = await run_workstream_assignment(session)
            logger.info("Workstream assignment: %s", stats)

    except Exception:
        logger.exception("Processing cycle failed")


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

    # ── Start background services ────────────────────────
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from aegis.ingestion.poller import start_polling, stop_polling

    # Start data pollers (calendar, email, Teams)
    await start_polling()

    # Start the 30-minute processing cycle (triage → extraction → workstream)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_processing_cycle,
        "interval",
        seconds=1800,
        id="processing_cycle",
        replace_existing=True,
    )
    scheduler.start()
    app.state.scheduler = scheduler

    logger.info("Aegis ready — pollers and processing cycle running")
    yield

    # ── Shutdown ─────────────────────────────────────────
    logger.info("Aegis shutting down")
    scheduler.shutdown(wait=False)
    await stop_polling()
    await engine.dispose()


app = FastAPI(
    title="Aegis — AI Chief of Staff",
    version="0.1.0",
    lifespan=lifespan,
)

# ── Routers ──────────────────────────────────────────────
from aegis.web.routes.dashboard import router as dashboard_router  # noqa: E402
from aegis.web.routes.meetings import router as meetings_router  # noqa: E402
from aegis.web.routes.people import router as people_router  # noqa: E402
from aegis.web.routes.org_chart import router as org_chart_router  # noqa: E402
from aegis.web.routes.workstreams import router as workstreams_router  # noqa: E402
from aegis.web.routes.actions import router as actions_router  # noqa: E402
from aegis.web.routes.departments import router as departments_router  # noqa: E402
from aegis.web.routes.emails import router as emails_router  # noqa: E402
from aegis.web.routes.asks import router as asks_router  # noqa: E402
from aegis.web.routes.stubs import router as stubs_router  # noqa: E402

app.include_router(dashboard_router)
app.include_router(meetings_router)
app.include_router(people_router)
app.include_router(org_chart_router)
app.include_router(workstreams_router)
app.include_router(actions_router)
app.include_router(departments_router)
app.include_router(emails_router)
app.include_router(asks_router)
app.include_router(stubs_router)
