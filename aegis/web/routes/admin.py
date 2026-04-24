"""Admin settings page — ~70 configurable values in collapsible sections."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import AdminSetting, VoiceProfile
from aegis.web import templates

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Setting definitions ─────────────────────────────────────
# Each section: (section_title, description, [(key, label, description, input_type, default)])
# input_type: "number", "text", "bool", "time", "textarea"

def _build_sections():
    """Build the admin settings section definitions with defaults from config."""
    settings = get_settings()
    return [
        (
            "Connections",
            "External service connection parameters (read-only).",
            [
                ("screenpipe_url", "Screenpipe URL", "Screenpipe REST API endpoint", "readonly", settings.screenpipe_url),
                ("azure_client_id", "Azure Client ID", "Azure app registration client ID", "readonly", settings.azure_client_id),
                ("azure_tenant_id", "Azure Tenant ID", "Azure AD tenant ID", "readonly", settings.azure_tenant_id),
            ],
        ),
        (
            "Polling",
            "How often Aegis checks each data source for new content.",
            [
                ("polling_calendar_seconds", "Calendar Sync Interval", "Seconds between calendar sync runs", "number", settings.polling_calendar_seconds),
                ("polling_email_seconds", "Email Poll Interval", "Seconds between email polling runs", "number", settings.polling_email_seconds),
                ("polling_teams_seconds", "Teams Poll Interval", "Seconds between Teams polling runs", "number", settings.polling_teams_seconds),
                ("polling_screenpipe_seconds", "Screenpipe Poll Interval", "Seconds between Screenpipe checks", "number", settings.polling_screenpipe_seconds),
            ],
        ),
        (
            "Triage",
            "Thresholds for classifying items as substantive, contextual, or noise.",
            [
                ("triage_substantive_threshold", "Substantive Threshold", "Score above this = substantive (full extraction)", "number", settings.triage_substantive_threshold),
                ("triage_contextual_threshold", "Contextual Threshold", "Score above this = contextual (embedding only)", "number", settings.triage_contextual_threshold),
            ],
        ),
        (
            "Workstream Detection",
            "Controls for automatic workstream creation and item assignment.",
            [
                ("workstream_auto_create_confidence", "Auto-Create Confidence", "Minimum confidence to auto-create a workstream", "number", settings.workstream_auto_create_confidence),
                ("workstream_assign_high_confidence", "High Confidence Assign", "Auto-assign items above this confidence", "number", settings.workstream_assign_high_confidence),
                ("workstream_assign_low_confidence", "Low Confidence Assign", "Assign with flag above this confidence", "number", settings.workstream_assign_low_confidence),
                ("workstream_default_quiet_days", "Default Quiet Days", "Days of inactivity before auto-quiet", "number", settings.workstream_default_quiet_days),
            ],
        ),
        (
            "Meeting Processing",
            "Meeting filtering and exclusion settings.",
            [
                ("meeting_exclusion_keywords", "Exclusion Keywords", "Comma-separated keywords to auto-exclude meetings", "textarea", settings.meeting_exclusion_keywords),
            ],
        ),
        (
            "Intelligence Schedule",
            "When briefings and meeting prep notifications are generated.",
            [
                ("morning_briefing_time", "Morning Briefing Time", "Time for daily morning briefing (HH:MM)", "time", settings.morning_briefing_time),
                ("monday_brief_time", "Monday Brief Time", "Time for Monday weekly brief (HH:MM)", "time", settings.monday_brief_time),
                ("friday_recap_time", "Friday Recap Time", "Time for Friday weekly recap (HH:MM)", "time", settings.friday_recap_time),
                ("meeting_prep_minutes_before", "Meeting Prep Lead Time", "Minutes before meeting to show prep notification", "number", settings.meeting_prep_minutes_before),
            ],
        ),
        (
            "Notifications",
            "Toggle notification delivery channels.",
            [
                ("notify_macos", "macOS Notifications", "Show macOS native notifications for alerts", "bool", settings.notify_macos),
                ("notify_email_self", "Email to Self", "Send briefings to your own email", "bool", settings.notify_email_self),
                ("notify_teams_self", "Teams to Self", "Send briefings via Teams message to self", "bool", settings.notify_teams_self),
            ],
        ),
        (
            "Stale Thresholds",
            "When items are considered stale and trigger nudges.",
            [
                ("stale_action_item_days", "Stale Action Item Days", "Days before an action item is marked stale", "number", settings.stale_action_item_days),
                ("stale_ask_hours", "Stale Ask Hours", "Hours before an unanswered ask is flagged", "number", settings.stale_ask_hours),
                ("stale_nudge_threshold_days", "Nudge Threshold Days", "Days before auto-generating a nudge draft", "number", settings.stale_nudge_threshold_days),
            ],
        ),
        (
            "Noise Filtering",
            "Controls for pre-filtering low-value content.",
            [
                ("email_skip_noreply", "Skip No-Reply Emails", "Auto-classify no-reply sender emails as automated", "bool", settings.email_skip_noreply),
                ("teams_min_message_length", "Teams Min Message Length", "Minimum character length for Teams messages", "number", settings.teams_min_message_length),
                ("teams_channel_batch_minutes", "Channel Batch Window", "Minutes to batch channel messages together", "number", settings.teams_channel_batch_minutes),
            ],
        ),
        (
            "Sentiment",
            "Sentiment analysis and friction detection parameters.",
            [
                ("sentiment_rolling_window_days", "Rolling Window Days", "Days for sentiment rolling average", "number", settings.sentiment_rolling_window_days),
                ("sentiment_trend_window_days", "Trend Window Days", "Days for trend comparison", "number", settings.sentiment_trend_window_days),
                ("sentiment_friction_threshold", "Friction Threshold", "Score below this triggers friction alert (0-100)", "number", settings.sentiment_friction_threshold),
            ],
        ),
        (
            "Data Retention",
            "How long data is kept at different tiers.",
            [
                ("retention_hot_days", "Hot Retention Days", "Days before moving data to warm storage", "number", settings.retention_hot_days),
                ("retention_warm_days", "Warm Retention Days", "Days before archiving data", "number", settings.retention_warm_days),
            ],
        ),
        (
            "Dashboard",
            "Dashboard display and cache settings.",
            [
                ("dashboard_cache_ttl_seconds", "Cache TTL Seconds", "Seconds before dashboard cache expires", "number", settings.dashboard_cache_ttl_seconds),
                ("dashboard_max_workstream_slots", "Max Workstream Slots", "Maximum workstream cards on dashboard", "number", settings.dashboard_max_workstream_slots),
            ],
        ),
        (
            "Readiness",
            "Thresholds for workload balance scoring bands.",
            [
                ("readiness_light_max", "Light Max Score", "Score ceiling for 'light' workload band", "number", settings.readiness_light_max),
                ("readiness_moderate_max", "Moderate Max Score", "Score ceiling for 'moderate' workload band", "number", settings.readiness_moderate_max),
                ("readiness_heavy_max", "Heavy Max Score", "Score ceiling for 'heavy' workload band", "number", settings.readiness_heavy_max),
            ],
        ),
    ]


async def _get_admin_values(session: AsyncSession) -> dict[str, object]:
    """Load all admin_settings overrides into a dict."""
    stmt = select(AdminSetting)
    result = await session.execute(stmt)
    overrides: dict[str, object] = {}
    for row in result.scalars().all():
        overrides[row.key] = row.value
    return overrides


def _resolve_value(key: str, default: object, overrides: dict[str, object]) -> object:
    """Return the admin_settings override if present, else the config default."""
    if key in overrides:
        val = overrides[key]
        # Handle JSONB dict formats: {"v": actual} or {"value": actual}
        if isinstance(val, dict):
            if "v" in val:
                return val["v"]
            if "value" in val:
                return val["value"]
        # Handle double-encoded JSON strings from earlier bootstrap
        if isinstance(val, str):
            try:
                import json
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    return parsed.get("v", parsed.get("value", val))
                return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return val
    return default


# ── Routes ──────────────────────────────────────────────────


@router.get("/admin")
async def admin_page(request: Request, session: AsyncSession = Depends(get_session)):
    overrides = await _get_admin_values(session)
    sections = _build_sections()

    # Build sections with resolved values
    resolved_sections = []
    for title, description, fields in sections:
        resolved_fields = []
        for key, label, desc, input_type, default in fields:
            current = _resolve_value(key, default, overrides)
            resolved_fields.append({
                "key": key,
                "label": label,
                "description": desc,
                "input_type": input_type,
                "value": current,
                "default": default,
            })
        resolved_sections.append({
            "title": title,
            "description": description,
            "fields": resolved_fields,
        })

    # Load voice profile
    vp_stmt = select(VoiceProfile).limit(1)
    vp_result = await session.execute(vp_stmt)
    voice_profile = vp_result.scalar_one_or_none()

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "sections": resolved_sections,
            "voice_profile": voice_profile,
            "current_time": "",
        },
    )


@router.post("/admin/settings")
async def save_setting(
    request: Request,
    key: str = Form(...),
    value: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Save a single admin setting via HTMX auto-save."""
    # Parse value to appropriate type
    parsed: object
    if value.lower() in ("true", "false"):
        parsed = value.lower() == "true"
    else:
        try:
            # Try int first, then float
            if "." in value:
                parsed = float(value)
            else:
                parsed = int(value)
        except ValueError:
            parsed = value

    now = datetime.now(timezone.utc)
    stmt = pg_insert(AdminSetting).values(
        key=key,
        value={"v": parsed},
        updated=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"value": {"v": parsed}, "updated": now},
    )
    await session.execute(stmt)
    await session.commit()

    return templates.TemplateResponse(
        request,
        "components/save_indicator.html",
        {"status": "saved"},
    )


@router.post("/admin/voice/regenerate")
async def regenerate_voice(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Trigger voice profile re-learning from sent emails."""
    try:
        from aegis.intelligence.voice_profile import learn_voice
        await learn_voice(session)

        # Reload profile
        vp_stmt = select(VoiceProfile).limit(1)
        vp_result = await session.execute(vp_stmt)
        voice_profile = vp_result.scalar_one_or_none()

        return templates.TemplateResponse(
            request,
            "components/voice_profile_card.html",
            {"voice_profile": voice_profile, "message": "Voice profile regenerated successfully."},
        )
    except Exception:
        logger.exception("Voice profile regeneration failed")
        return templates.TemplateResponse(
            request,
            "components/voice_profile_card.html",
            {"voice_profile": None, "message": "Regeneration failed. Check logs for details."},
        )


@router.post("/admin/voice/rule")
async def add_voice_rule(
    request: Request,
    rule: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Add a custom voice rule."""
    vp_stmt = select(VoiceProfile).limit(1)
    vp_result = await session.execute(vp_stmt)
    voice_profile = vp_result.scalar_one_or_none()

    if not voice_profile:
        voice_profile = VoiceProfile(
            auto_profile=None,
            custom_rules=[rule.strip()],
            edit_history={},
            updated=datetime.now(timezone.utc),
        )
        session.add(voice_profile)
    else:
        existing_rules = list(voice_profile.custom_rules or [])
        existing_rules.append(rule.strip())
        voice_profile.custom_rules = existing_rules
        voice_profile.updated = datetime.now(timezone.utc)

    await session.commit()

    return templates.TemplateResponse(
        request,
        "components/voice_profile_card.html",
        {"voice_profile": voice_profile, "message": "Rule added."},
    )


@router.delete("/admin/voice/rule/{index}")
async def delete_voice_rule(
    request: Request,
    index: int,
    session: AsyncSession = Depends(get_session),
):
    """Remove a custom voice rule by index."""
    vp_stmt = select(VoiceProfile).limit(1)
    vp_result = await session.execute(vp_stmt)
    voice_profile = vp_result.scalar_one_or_none()

    if voice_profile and voice_profile.custom_rules:
        rules = list(voice_profile.custom_rules)
        if 0 <= index < len(rules):
            rules.pop(index)
            voice_profile.custom_rules = rules
            voice_profile.updated = datetime.now(timezone.utc)
            await session.commit()

    return templates.TemplateResponse(
        request,
        "components/voice_profile_card.html",
        {"voice_profile": voice_profile, "message": "Rule removed."},
    )
