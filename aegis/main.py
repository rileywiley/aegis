"""Aegis — AI Chief of Staff. FastAPI application entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from aegis.clients.helios import HeliosClient
from aegis.config import get_settings
from aegis.db.engine import async_session_factory, engine
from aegis.db.repositories import reset_stuck_processing
from aegis.ingestion.helios_heartbeat import helios_heartbeat_loop

logger = logging.getLogger("aegis")

settings = get_settings()

_LOG_DIR = Path.home() / ".aegis" / "logs"
_LOG_FILE = _LOG_DIR / "aegis.log"
_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_LOG_BACKUP_COUNT = 5

# Ensure log directory exists at module load time (not just in lifespan)
_LOG_DIR.mkdir(parents=True, exist_ok=True)


async def _run_processing_cycle(helios_client: HeliosClient | None = None) -> None:
    """30-minute processing cycle: triage → extraction → workstream assignment.

    Must run sequentially — each step depends on the previous one's output.
    ``helios_client`` is passed via APScheduler args so we can build pending
    transcripts before the extraction step runs.
    """
    from datetime import datetime, timedelta, timezone

    from aegis.processing.triage import triage_batch, apply_triage_results
    from aegis.processing.pipeline import (
        process_pending_meetings,
        process_pending_voice_notes,
    )
    from aegis.processing.workstream_detector import run_workstream_assignment
    from aegis.ingestion.meeting_detector import MeetingDetector

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

        # Step 1b: Build transcripts for any completed meetings whose audio
        # Helios has captured but Aegis hasn't ingested yet. Must run BEFORE
        # extraction so freshly-built transcripts get picked up in the same
        # cycle.
        if helios_client is not None:
            async with async_session_factory() as session:
                try:
                    detector = MeetingDetector(helios_client)
                    built = await detector.process_completed_meetings(session)
                    if built:
                        logger.info("Transcript builder: built %d transcripts", built)
                except Exception:
                    logger.exception("Transcript builder failed")
                    await session.rollback()

        # Step 2: Run extraction pipeline on pending meetings
        count = await process_pending_meetings()
        if count:
            logger.info("Extraction: processed %d meetings", count)

        # Step 2a: Run extraction pipeline on pending voice notes
        try:
            vn_count = await process_pending_voice_notes()
            if vn_count:
                logger.info("Extraction: processed %d voice notes", vn_count)
        except Exception:
            logger.exception("Voice note extraction failed")

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

        # Step 7: Retry missing embeddings (items that got zero vectors from API failures)
        try:
            from aegis.processing.embeddings import regenerate_missing_embeddings
            async with async_session_factory() as session:
                regen_count = await regenerate_missing_embeddings(session)
                if regen_count:
                    logger.info("Regenerated %d missing embeddings", regen_count)
        except Exception:
            logger.debug("Embedding regeneration failed", exc_info=True)

        # Step 8: Update system_health for processing services
        from aegis.db.repositories import upsert_system_health

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

    # Bootstrap admin settings (pre-populate from .env on first run)
    from aegis.db.admin_config import bootstrap_admin_settings, load_admin_overrides
    async with async_session_factory() as session:
        bootstrapped = await bootstrap_admin_settings(session)
        if bootstrapped:
            logger.info("Bootstrapped %d admin settings from config defaults", bootstrapped)
        overrides = await load_admin_overrides(session)
        if overrides:
            logger.info("Loaded %d admin setting overrides", overrides)

    # Populate nav badge counts
    from aegis.web.nav_counts import get_nav_counts
    await get_nav_counts()

    # ── Start background services ────────────────────────
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from aegis.ingestion.poller import start_polling, stop_polling

    # Helios capture daemon client + heartbeat (HELIOS.md §16.9).
    # The httpx.AsyncClient is owned by the lifespan; HeliosClient
    # does NOT close it, so we tear it down explicitly on shutdown.
    helios_http = httpx.AsyncClient()
    helios_client = HeliosClient(
        base_url=settings.helios_url,
        token_path=settings.helios_token_path,
        http=helios_http,
        timeout_seconds=float(settings.helios_heartbeat_timeout_seconds),
    )
    app.state.helios_client = helios_client
    app.state.helios_http = helios_http

    # Resolve the macOS notifier eagerly so non-macOS hosts (CI, Linux
    # remotes) don't crash on the first ``healthy → down`` edge — which
    # would otherwise be the worst possible time to discover a broken
    # notifier. On ImportError fall back to a no-op that just logs.
    try:
        from aegis.notifications.macos import notify as _helios_notifier
    except ImportError as exc:  # pragma: no cover — exercised on non-mac
        logger.warning(
            "helios_notifier_unavailable",
            extra={"error": str(exc)},
        )

        async def _helios_notifier(_title: str, _message: str) -> None:
            # No-op fallback: not a macOS host, nothing to do.
            return None

    helios_heartbeat_task = asyncio.create_task(
        helios_heartbeat_loop(
            helios_client,
            async_session_factory,
            settings,
            notifier=_helios_notifier,
        )
    )
    app.state.helios_heartbeat_task = helios_heartbeat_task

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
        args=[helios_client],
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

    # Daily database backup (2 AM)
    async def _run_backup():
        import subprocess
        try:
            subprocess.run(
                ["python", "scripts/backup.py", "--rotate"],
                capture_output=True, timeout=120,
            )
            logger.info("Daily backup completed")
        except Exception:
            logger.exception("Daily backup failed")

    scheduler.add_job(
        _run_backup, "cron", hour=2, minute=0,
        id="daily_backup", replace_existing=True,
    )

    scheduler.start()
    app.state.scheduler = scheduler

    logger.info("Aegis ready — pollers, processing cycle, and intelligence jobs running")
    yield

    # ── Shutdown ─────────────────────────────────────────
    logger.info("Aegis shutting down")
    scheduler.shutdown(wait=False)
    await stop_polling()

    # Cancel + drain the Helios heartbeat loop, then close its httpx client.
    helios_heartbeat_task.cancel()
    try:
        await helios_heartbeat_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await helios_http.aclose()
    except Exception:
        logger.debug("helios_http_close_failed", exc_info=True)

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
from aegis.web.routes.decisions import router as decisions_router  # noqa: E402
from aegis.web.routes.departments import router as departments_router  # noqa: E402
from aegis.web.routes.readiness import router as readiness_router  # noqa: E402
from aegis.web.routes.emails import router as emails_router  # noqa: E402
from aegis.web.routes.asks import router as asks_router  # noqa: E402
from aegis.web.routes.respond import router as respond_router  # noqa: E402
from aegis.web.routes.chat import router as chat_router  # noqa: E402
from aegis.web.routes.admin import router as admin_router  # noqa: E402
from aegis.web.routes.search import router as search_router  # noqa: E402
from aegis.web.routes.stubs import router as stubs_router  # noqa: E402
from aegis.web.routes.api import router as api_router  # noqa: E402
from aegis.web.routes.voice_notes import router as voice_notes_router  # noqa: E402
from aegis.web.routes.helios import router as helios_router  # noqa: E402

app.include_router(dashboard_router)
app.include_router(meetings_router)
app.include_router(people_router)
app.include_router(org_chart_router)
app.include_router(workstreams_router)
app.include_router(actions_router)
app.include_router(decisions_router)
app.include_router(departments_router)
app.include_router(readiness_router)
app.include_router(emails_router)
app.include_router(asks_router)
app.include_router(respond_router)
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(search_router)
app.include_router(stubs_router)
app.include_router(api_router)
app.include_router(voice_notes_router)
app.include_router(helios_router)
