"""Aegis — AI Chief of Staff. FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import text

from aegis.config import get_settings
from aegis.db.engine import async_session_factory, engine
from aegis.db.repositories import reset_stuck_processing

logger = logging.getLogger("aegis")

settings = get_settings()

_LOG_DIR = Path.home() / ".aegis" / "logs"
_LOG_FILE = _LOG_DIR / "aegis.log"
_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_LOG_BACKUP_COUNT = 5


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
                    await session.rollback()
                    await session.execute(
                        update(Email).where(Email.id == email.id)
                        .values(processing_status="failed")
                    )
                    await session.commit()

            if substantive_emails:
                logger.info("Extraction: processed %d substantive emails", len(substantive_emails))

        # Step 2d: Extract substantive chat messages
        from aegis.processing.chat_extractor import extract_chat, store_chat_extraction

        async with async_session_factory() as session:
            from sqlalchemy import select, update
            from aegis.db.models import ChatMessage

            stmt = select(ChatMessage).where(
                ChatMessage.triage_class == "substantive",
                ChatMessage.processing_status == "pending",
            ).limit(10)  # Limit per cycle to control cost
            result = await session.execute(stmt)
            substantive_chats = list(result.scalars().all())

            for chat_msg in substantive_chats:
                try:
                    await session.execute(
                        update(ChatMessage).where(ChatMessage.id == chat_msg.id)
                        .values(processing_status="processing")
                    )
                    await session.commit()

                    extraction = await extract_chat(session, chat_msg.id)
                    if extraction:
                        await resolve_extracted_entities(session, 0, extraction)
                        await store_chat_extraction(session, chat_msg.id, extraction)

                    await session.execute(
                        update(ChatMessage).where(ChatMessage.id == chat_msg.id)
                        .values(processing_status="completed")
                    )
                    await session.commit()
                except Exception:
                    logger.exception("Chat extraction failed for message %d", chat_msg.id)
                    await session.rollback()
                    await session.execute(
                        update(ChatMessage).where(ChatMessage.id == chat_msg.id)
                        .values(processing_status="failed")
                    )
                    await session.commit()

            if substantive_chats:
                logger.info("Extraction: processed %d substantive chat messages", len(substantive_chats))

        # Step 2e: Generate embeddings for contextual chat messages (no LLM extraction)
        from aegis.processing.embeddings import embed_text

        async with async_session_factory() as session:
            from sqlalchemy import select, update
            from aegis.db.models import ChatMessage

            stmt = select(ChatMessage).where(
                ChatMessage.triage_class == "contextual",
                ChatMessage.embedding.is_(None),
                ChatMessage.noise_filtered.is_(False),
            ).limit(50)
            result = await session.execute(stmt)
            chats_needing_embed = list(result.scalars().all())

            for chat in chats_needing_embed:
                try:
                    text = chat.body_text or chat.body_preview or ""
                    if text.strip():
                        emb = await embed_text(text[:2000])
                        await session.execute(
                            update(ChatMessage).where(ChatMessage.id == chat.id)
                            .values(embedding=emb, processing_status="completed")
                        )
                    else:
                        await session.execute(
                            update(ChatMessage).where(ChatMessage.id == chat.id)
                            .values(processing_status="completed")
                        )
                except Exception:
                    logger.debug("Chat embedding failed for %d", chat.id)

            if chats_needing_embed:
                await session.commit()
                logger.info("Embeddings: generated for %d chat messages", len(chats_needing_embed))

        # Step 3: Workstream assignment
        async with async_session_factory() as session:
            stats = await run_workstream_assignment(session)
            logger.info("Workstream assignment: %s", stats)

        # Step 5: Generate nudge drafts for stale items
        from aegis.intelligence.draft_generator import generate_stale_nudges

        async with async_session_factory() as session:
            try:
                nudge_count = await generate_stale_nudges(session)
                if nudge_count:
                    logger.info("Generated %d stale item nudges", nudge_count)
            except Exception:
                logger.exception("Nudge generation failed")
                await session.rollback()

        # Step 6: Parse email signatures (once per day)
        from aegis.processing.org_inference import parse_email_signatures

        async with async_session_factory() as session:
            try:
                from sqlalchemy import select
                from aegis.db.models import SystemHealth as SH
                sig_stmt = select(SH).where(SH.service == "signature_parser")
                sig_result = await session.execute(sig_stmt)
                sig_health = sig_result.scalar_one_or_none()
                run_sigs = True
                if sig_health and sig_health.last_success:
                    from datetime import timedelta
                    last = sig_health.last_success.replace(
                        tzinfo=timezone.utc if sig_health.last_success.tzinfo is None else sig_health.last_success.tzinfo
                    )
                    if (datetime.now(timezone.utc) - last) < timedelta(hours=24):
                        run_sigs = False
                if run_sigs:
                    sig_stats = await parse_email_signatures(session)
                    logger.info("Signature parsing: %s", sig_stats)
                    from aegis.db.repositories import upsert_system_health as _ush
                    await _ush(session, "signature_parser", status="healthy", last_success=datetime.now(timezone.utc))
            except Exception:
                logger.exception("Signature parsing failed")
                await session.rollback()

        # Step 7: Update system_health for processing services
        from aegis.db.repositories import upsert_system_health
        from datetime import datetime, timezone

        async with async_session_factory() as session:
            now = datetime.now(timezone.utc)
            await upsert_system_health(session, "triage_batch", status="healthy", last_success=now)
            await upsert_system_health(session, "extraction_pipeline", status="healthy", last_success=now)
            await upsert_system_health(session, "workstream_detector", status="healthy", last_success=now)

    except Exception:
        logger.exception("Processing cycle failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # ── Startup ──────────────────────────────────────────
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    log_format = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    log_datefmt = "%Y-%m-%d %H:%M:%S"

    # Console handler (existing behaviour)
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=log_datefmt,
    )

    # Rotating file handler — logs to ~/.aegis/logs/aegis.log
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=log_datefmt))
    logging.getLogger().addHandler(file_handler)

    logger.info("Aegis starting on %s:%s  tz=%s", settings.aegis_host, settings.aegis_port, settings.aegis_timezone)
    logger.info("Log file: %s", _LOG_FILE)

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

    # Dashboard cache refresh every 15 min
    from aegis.web.routes.dashboard import refresh_dashboard_cache
    scheduler.add_job(
        refresh_dashboard_cache,
        "interval",
        seconds=settings.dashboard_cache_ttl_seconds,
        id="dashboard_cache_refresh",
        replace_existing=True,
    )

    # Intelligence jobs (briefings, meeting prep notifications)
    from aegis.intelligence.scheduler import register_intelligence_jobs
    register_intelligence_jobs(scheduler)

    scheduler.start()
    app.state.scheduler = scheduler

    logger.info("Aegis ready — pollers, processing cycle, and intelligence jobs running")
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
from aegis.web.routes.readiness import router as readiness_router  # noqa: E402
from aegis.web.routes.emails import router as emails_router  # noqa: E402
from aegis.web.routes.asks import router as asks_router  # noqa: E402
from aegis.web.routes.respond import router as respond_router  # noqa: E402
from aegis.web.routes.chat import router as chat_router  # noqa: E402
from aegis.web.routes.admin import router as admin_router  # noqa: E402
from aegis.web.routes.search import router as search_router  # noqa: E402
from aegis.web.routes.stubs import router as stubs_router  # noqa: E402

app.include_router(dashboard_router)
app.include_router(meetings_router)
app.include_router(people_router)
app.include_router(org_chart_router)
app.include_router(workstreams_router)
app.include_router(actions_router)
app.include_router(departments_router)
app.include_router(readiness_router)
app.include_router(emails_router)
app.include_router(asks_router)
app.include_router(respond_router)
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(search_router)
app.include_router(stubs_router)
