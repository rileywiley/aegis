"""Admin settings runtime override — reads from admin_settings table, falls back to config.py."""

import json
import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import AdminSetting

logger = logging.getLogger(__name__)

# In-memory cache of admin overrides (refreshed on startup and on save)
_admin_overrides: dict[str, Any] = {}


async def get_runtime_setting(session: AsyncSession, key: str) -> Any:
    """Get a setting value: admin_settings override first, then config.py default."""
    # Check in-memory cache first
    if key in _admin_overrides:
        return _admin_overrides[key]

    # Check DB
    stmt = select(AdminSetting.value).where(AdminSetting.key == key)
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is not None:
        # admin_settings stores JSONB — extract the actual value
        val = row if not isinstance(row, dict) else row.get("value", row)
        _admin_overrides[key] = val
        return val

    # Fall back to config.py
    settings = get_settings()
    return getattr(settings, key, None)


async def load_admin_overrides(session: AsyncSession) -> int:
    """Load all admin_settings into memory cache. Called on startup."""
    global _admin_overrides
    stmt = select(AdminSetting)
    result = await session.execute(stmt)
    settings = list(result.scalars().all())

    _admin_overrides = {}
    for s in settings:
        val = s.value
        if isinstance(val, dict) and "value" in val:
            val = val["value"]
        _admin_overrides[s.key] = val

    logger.info("Loaded %d admin setting overrides", len(_admin_overrides))
    return len(_admin_overrides)


async def bootstrap_admin_settings(session: AsyncSession) -> int:
    """Pre-populate admin_settings with current config.py values if table is empty.

    Only runs on first startup when the table is empty.
    """
    # Check if table already has data
    count_result = await session.execute(text("SELECT COUNT(*) FROM admin_settings"))
    count = count_result.scalar_one()
    if count > 0:
        return 0  # Already populated

    settings = get_settings()

    # Map of setting key → (value, description)
    setting_defs = {
        # Polling
        "polling_calendar_seconds": (settings.polling_calendar_seconds, "Calendar sync interval (seconds)"),
        "polling_email_seconds": (settings.polling_email_seconds, "Email polling interval (seconds)"),
        "polling_teams_seconds": (settings.polling_teams_seconds, "Teams polling interval (seconds)"),
        "polling_screenpipe_seconds": (settings.polling_screenpipe_seconds, "Screenpipe polling interval (seconds)"),
        # Triage
        "triage_substantive_threshold": (settings.triage_substantive_threshold, "Score above which items are substantive"),
        "triage_contextual_threshold": (settings.triage_contextual_threshold, "Score above which items are contextual"),
        # Workstream
        "workstream_auto_create_confidence": (settings.workstream_auto_create_confidence, "Min confidence for auto workstream creation"),
        "workstream_assign_high_confidence": (settings.workstream_assign_high_confidence, "Auto-assign threshold"),
        "workstream_assign_low_confidence": (settings.workstream_assign_low_confidence, "Low-confidence assign threshold"),
        "workstream_default_quiet_days": (settings.workstream_default_quiet_days, "Days of inactivity before auto-quiet"),
        # Stale thresholds
        "stale_action_item_days": (settings.stale_action_item_days, "Days before action item is stale"),
        "stale_ask_hours": (settings.stale_ask_hours, "Hours before ask is stale"),
        "stale_nudge_threshold_days": (settings.stale_nudge_threshold_days, "Days before nudge draft is generated"),
        # Intelligence schedule
        "morning_briefing_time": (settings.morning_briefing_time, "Daily briefing generation time (HH:MM)"),
        "monday_brief_time": (settings.monday_brief_time, "Monday brief generation time (HH:MM)"),
        "friday_recap_time": (settings.friday_recap_time, "Friday recap generation time (HH:MM)"),
        "meeting_prep_minutes_before": (settings.meeting_prep_minutes_before, "Minutes before meeting to notify"),
        # Notifications
        "notify_macos": (settings.notify_macos, "Enable macOS desktop notifications"),
        "notify_email_self": (settings.notify_email_self, "Email briefings to yourself"),
        "notify_teams_self": (settings.notify_teams_self, "Send briefings via Teams"),
        # Noise filtering
        "email_skip_noreply": (settings.email_skip_noreply, "Auto-classify noreply senders"),
        "teams_min_message_length": (settings.teams_min_message_length, "Min chat message length to keep"),
        "teams_channel_batch_minutes": (settings.teams_channel_batch_minutes, "Channel message batch window"),
        # Sentiment
        "sentiment_rolling_window_days": (settings.sentiment_rolling_window_days, "Sentiment rolling window (days)"),
        "sentiment_trend_window_days": (settings.sentiment_trend_window_days, "Trend comparison window (days)"),
        "sentiment_friction_threshold": (settings.sentiment_friction_threshold, "Friction detection threshold"),
        # Retention
        "retention_hot_days": (settings.retention_hot_days, "Hot tier retention (days)"),
        "retention_warm_days": (settings.retention_warm_days, "Warm tier retention (days)"),
        # Dashboard
        "dashboard_cache_ttl_seconds": (settings.dashboard_cache_ttl_seconds, "Dashboard cache TTL (seconds)"),
        "dashboard_max_workstream_slots": (settings.dashboard_max_workstream_slots, "Max workstream cards on dashboard"),
        # Readiness
        "readiness_light_max": (settings.readiness_light_max, "Light workload max score"),
        "readiness_moderate_max": (settings.readiness_moderate_max, "Moderate workload max score"),
        "readiness_heavy_max": (settings.readiness_heavy_max, "Heavy workload max score"),
        # Meeting
        "meeting_exclusion_keywords": (settings.meeting_exclusion_keywords, "Comma-separated keywords to exclude meetings"),
    }

    inserted = 0
    from datetime import datetime, timezone

    for key, (value, description) in setting_defs.items():
        stmt = pg_insert(AdminSetting).values(
            key=key,
            value=json.dumps({"value": value}),
            description=description,
            updated=datetime.now(timezone.utc),
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["key"])
        await session.execute(stmt)
        inserted += 1

    await session.commit()
    logger.info("Bootstrapped %d admin settings from config.py defaults", inserted)
    return inserted
