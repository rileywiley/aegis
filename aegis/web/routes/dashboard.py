"""Dashboard — Command Center route."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.repositories import get_meetings_for_range
from aegis.web import templates

router = APIRouter()
settings = get_settings()


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


def _today_range_utc() -> tuple[datetime, datetime]:
    """Return UTC start/end for today in the configured local timezone."""
    tz = _local_tz()
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


@router.get("/")
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    start_utc, end_utc = _today_range_utc()
    meetings = await get_meetings_for_range(session, start_utc, end_utc)

    tz = _local_tz()
    now_local = datetime.now(tz)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "meetings": meetings,
            "today_str": now_local.strftime("%A, %B %-d, %Y"),
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
        },
    )


@router.get("/api/meetings-today")
async def meetings_today_partial(request: Request, session: AsyncSession = Depends(get_session)):
    """HTMX partial — returns just the meetings list HTML fragment."""
    start_utc, end_utc = _today_range_utc()
    meetings = await get_meetings_for_range(session, start_utc, end_utc)
    tz = _local_tz()

    return templates.TemplateResponse(
        request,
        "components/meetings_today.html",
        {
            "meetings": meetings,
            "tz": tz,
        },
    )
