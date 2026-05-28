"""Off-hours LLM cost control gate.

Single ``llm_calls_allowed()`` helper, consulted at the top of scheduled
job bodies (``processing_cycle`` + briefing crons) to skip Anthropic
spend outside a configurable working-hours window.

User-initiated paths (``/ask`` chat, re-extract, voice notes, workstream
rename) intentionally do NOT call this — they're explicit user
actions and the spend is wanted.

Precedence (top wins):

1. ``llm_force_active = True`` — bypass everything, calls allowed.
2. ``llm_force_pause = True`` — kill switch, calls denied.
3. ``llm_work_hours_enabled = False`` — gate disabled, calls allowed.
4. Clock check against ``[llm_work_hours_start, llm_work_hours_end)``
   on ``llm_work_days`` (ISO day numbers Mon=1..Sun=7), evaluated in
   the ``aegis_timezone``.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import AdminSetting

logger = logging.getLogger(__name__)


# JSONB storage shapes used in admin_settings: ``{"v": x}`` is the
# current bootstrap shape; ``{"value": x}`` is a legacy shape from
# earlier code. We tolerate both.
def _unwrap(raw: Any) -> Any:
    if isinstance(raw, dict):
        if "v" in raw:
            return raw["v"]
        if "value" in raw:
            return raw["value"]
    return raw


async def _read_settings(
    session: AsyncSession, keys: list[str]
) -> dict[str, Any]:
    """Bulk-read settings from admin_settings, falling back to config.py.

    Single query keeps the gate cheap to call from every scheduled job.
    """
    defaults = get_settings()
    out: dict[str, Any] = {k: getattr(defaults, k, None) for k in keys}
    stmt = select(AdminSetting.key, AdminSetting.value).where(
        AdminSetting.key.in_(keys)
    )
    for key, raw in (await session.execute(stmt)).all():
        out[key] = _unwrap(raw)
    return out


def _parse_time(value: Any, fallback: time) -> time:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        logger.warning("llm_gate_bad_time_value", extra={"value": value})
        return fallback


def _parse_work_days(value: Any) -> set[int]:
    """Parse '1,2,3,4,5' into {1,2,3,4,5} (ISO day numbers, Mon=1..Sun=7)."""
    if not isinstance(value, str) or not value.strip():
        return set()
    days: set[int] = set()
    for part in value.split(","):
        try:
            n = int(part.strip())
        except ValueError:
            continue
        if 1 <= n <= 7:
            days.add(n)
    return days


def _next_open_at(
    now_local: datetime,
    start_t: time,
    end_t: time,
    work_days: set[int],
) -> datetime | None:
    """Compute the next datetime the gate opens, for the dashboard pill."""
    if not work_days:
        return None
    candidate = now_local.replace(
        hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0
    )
    # Same day if start time is still ahead.
    if candidate > now_local and now_local.isoweekday() in work_days:
        return candidate
    # Otherwise scan forward up to 14 days for the next work day.
    from datetime import timedelta

    for delta in range(1, 14):
        nxt = candidate + timedelta(days=delta)
        if nxt.isoweekday() in work_days:
            return nxt
    return None


async def llm_calls_allowed(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)``.

    ``reason`` is a human-readable string suitable for log fields and the
    dashboard "Service health" pill (e.g. "outside work hours — next
    active Mon 08:00").
    """
    keys = [
        "llm_force_active",
        "llm_force_pause",
        "llm_work_hours_enabled",
        "llm_work_hours_start",
        "llm_work_hours_end",
        "llm_work_days",
    ]
    cfg = await _read_settings(session, keys)

    if bool(cfg.get("llm_force_active")):
        return True, "force-active override set"
    if bool(cfg.get("llm_force_pause")):
        return False, "force-paused"
    if not bool(cfg.get("llm_work_hours_enabled", True)):
        return True, "work-hours gate disabled"

    settings = get_settings()
    tz = ZoneInfo(settings.aegis_timezone or "UTC")
    now_local = (now or datetime.now(tz)).astimezone(tz)

    start_t = _parse_time(cfg.get("llm_work_hours_start"), time(8, 0))
    end_t = _parse_time(cfg.get("llm_work_hours_end"), time(18, 0))
    work_days = _parse_work_days(cfg.get("llm_work_days"))

    if not work_days:
        return False, "no work days configured"

    if start_t >= end_t:
        logger.warning(
            "llm_gate_invalid_window",
            extra={"start": str(start_t), "end": str(end_t)},
        )
        return False, "invalid work-hours window (start >= end)"

    is_work_day = now_local.isoweekday() in work_days
    in_window = start_t <= now_local.time() < end_t
    if is_work_day and in_window:
        return True, f"in work hours (open until {end_t.strftime('%H:%M')})"

    nxt = _next_open_at(now_local, start_t, end_t, work_days)
    if nxt is None:
        return False, "outside work hours"
    nxt_label = nxt.strftime("%a %H:%M")
    return False, f"outside work hours — next active {nxt_label}"


__all__ = ["llm_calls_allowed"]
