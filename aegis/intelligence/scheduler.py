"""APScheduler job definitions for intelligence layer.

Registers scheduled jobs for briefings, meeting prep notifications,
and other intelligence outputs. Called from main.py during startup.
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from aegis.config import get_settings
from aegis.db.engine import async_session_factory
from aegis.db.models import Briefing, Meeting

logger = logging.getLogger("aegis.scheduler")


async def _morning_briefing_job() -> None:
    """Daily morning briefing job. Uses Monday brief on Mondays."""
    import zoneinfo

    from aegis.intelligence.briefings import (
        generate_monday_brief,
        generate_morning_briefing,
    )

    settings = get_settings()
    tz = zoneinfo.ZoneInfo(settings.aegis_timezone)
    local_now = datetime.now(tz)

    try:
        async with async_session_factory() as session:
            if local_now.weekday() == 0:  # Monday
                logger.info("Monday detected — generating Monday planning brief")
                await generate_monday_brief(session)
            else:
                logger.info("Generating morning briefing")
                await generate_morning_briefing(session)
    except Exception:
        logger.exception("Failed to generate morning briefing")


async def _friday_recap_job() -> None:
    """Friday end-of-week recap job."""
    from aegis.intelligence.briefings import generate_friday_recap

    try:
        async with async_session_factory() as session:
            logger.info("Generating Friday recap")
            await generate_friday_recap(session)
    except Exception:
        logger.exception("Failed to generate Friday recap")


async def _meeting_prep_notification_job() -> None:
    """Check for meetings starting within MEETING_PREP_MINUTES_BEFORE minutes.

    If a meeting prep brief exists, send a macOS notification linking to it.
    Runs every 5 minutes.
    """
    from aegis.notifications.macos import notify

    settings = get_settings()
    minutes_before = settings.meeting_prep_minutes_before
    now_utc = datetime.now(timezone.utc)
    window_start = now_utc + timedelta(minutes=1)  # at least 1 min from now
    window_end = now_utc + timedelta(minutes=minutes_before + 1)

    try:
        async with async_session_factory() as session:
            # Find meetings starting in the notification window
            stmt = (
                select(Meeting)
                .where(
                    Meeting.start_time >= window_start,
                    Meeting.start_time <= window_end,
                    Meeting.is_excluded.is_(False),
                )
                .order_by(Meeting.start_time)
            )
            result = await session.execute(stmt)
            upcoming = list(result.scalars().all())

            for meeting in upcoming:
                # Check if we have a prep brief
                prep_stmt = select(Briefing).where(
                    Briefing.briefing_type == "meeting_prep",
                    Briefing.related_meeting_id == meeting.id,
                )
                prep_result = await session.execute(prep_stmt)
                has_prep = prep_result.scalar_one_or_none() is not None

                # Calculate minutes until meeting
                mins_until = int(
                    (meeting.start_time - now_utc).total_seconds() / 60
                )

                if has_prep:
                    await notify(
                        title=f"Meeting in {mins_until} min: {meeting.title}",
                        message="Meeting prep brief is ready. Open Aegis to review.",
                    )
                else:
                    # Try to generate prep on the fly
                    try:
                        from aegis.intelligence.meeting_prep import (
                            generate_meeting_prep,
                        )

                        await generate_meeting_prep(session, meeting.id)
                        await notify(
                            title=f"Meeting in {mins_until} min: {meeting.title}",
                            message="Meeting prep brief generated. Open Aegis to review.",
                        )
                    except Exception:
                        logger.exception(
                            "Failed to generate on-demand prep for meeting %d",
                            meeting.id,
                        )
                        await notify(
                            title=f"Meeting in {mins_until} min: {meeting.title}",
                            message="No meeting prep available.",
                        )

    except Exception:
        logger.exception("Meeting prep notification check failed")


async def _sentiment_aggregation_job() -> None:
    """Compute sentiment aggregations for all scopes."""
    try:
        from aegis.intelligence.sentiment import compute_sentiment_aggregations

        async with async_session_factory() as session:
            count = await compute_sentiment_aggregations(session)
            logger.info("Sentiment aggregation complete — %d rows upserted", count)
    except Exception:
        logger.exception("Sentiment aggregation job failed")


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' string into (hour, minute)."""
    parts = time_str.strip().split(":")
    return int(parts[0]), int(parts[1])


def register_intelligence_jobs(scheduler: AsyncIOScheduler) -> None:
    """Register all intelligence-layer scheduled jobs on the given scheduler.

    Called from main.py during app startup. Jobs use cron triggers with
    the configured timezone.
    """
    settings = get_settings()

    morning_hour, morning_minute = _parse_time(settings.morning_briefing_time)
    friday_hour, friday_minute = _parse_time(settings.friday_recap_time)

    # Morning briefing: daily (Monday variant auto-detected inside the job)
    scheduler.add_job(
        _morning_briefing_job,
        "cron",
        hour=morning_hour,
        minute=morning_minute,
        timezone=settings.aegis_timezone,
        id="morning_briefing",
        replace_existing=True,
        misfire_grace_time=3600,  # 1 hour grace if system was asleep
    )
    logger.info(
        "Scheduled morning briefing at %02d:%02d %s",
        morning_hour,
        morning_minute,
        settings.aegis_timezone,
    )

    # Friday recap: Fridays only
    scheduler.add_job(
        _friday_recap_job,
        "cron",
        day_of_week="fri",
        hour=friday_hour,
        minute=friday_minute,
        timezone=settings.aegis_timezone,
        id="friday_recap",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info(
        "Scheduled Friday recap at %02d:%02d %s (Fridays)",
        friday_hour,
        friday_minute,
        settings.aegis_timezone,
    )

    # Meeting prep notifications: every 5 minutes
    scheduler.add_job(
        _meeting_prep_notification_job,
        "interval",
        minutes=5,
        id="meeting_prep_notifications",
        replace_existing=True,
    )
    logger.info("Scheduled meeting prep notifications (every 5 min)")

    # Sentiment aggregation: every 6 hours
    scheduler.add_job(
        _sentiment_aggregation_job,
        "interval",
        hours=6,
        id="sentiment_aggregation",
        replace_existing=True,
    )
    logger.info("Scheduled sentiment aggregation (every 6 hours)")
