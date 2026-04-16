"""Readiness page — workload balance and personnel readiness scoring."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import Person
from aegis.intelligence.readiness import (
    compute_all_readiness,
    get_readiness_detail,
)
from aegis.web import templates

router = APIRouter()
settings = get_settings()

VALID_SORT_COLS = {
    "name",
    "score",
    "open_items",
    "blocking_count",
    "workstream_count",
    "trend",
}


def _current_time() -> str:
    tz = ZoneInfo(settings.aegis_timezone)
    return datetime.now(tz).strftime("%-I:%M %p %Z")


def _score_color(score: int) -> str:
    """Return Tailwind color class based on readiness score thresholds."""
    if score <= settings.readiness_light_max:
        return "green"
    elif score <= settings.readiness_moderate_max:
        return "yellow"
    elif score <= settings.readiness_heavy_max:
        return "orange"
    return "red"


def _score_label(score: int) -> str:
    """Return human-readable label for readiness score."""
    if score <= settings.readiness_light_max:
        return "Light"
    elif score <= settings.readiness_moderate_max:
        return "Moderate"
    elif score <= settings.readiness_heavy_max:
        return "Heavy"
    return "Overloaded"


@router.get("/readiness")
async def readiness_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    sort: str = "score",
    order: str = "desc",
):
    """Readiness page with workload balance table."""
    # Compute scores for all active internal people with interactions
    scores = await compute_all_readiness(session)

    # Build display data by joining person info
    person_ids = [s.person_id for s in scores]
    persons_map: dict[int, Person] = {}
    if person_ids:
        stmt = select(Person).where(Person.id.in_(person_ids))
        result = await session.execute(stmt)
        for p in result.scalars().all():
            persons_map[p.id] = p

    rows = []
    for s in scores:
        person = persons_map.get(s.person_id)
        if not person:
            continue
        rows.append({
            "person": person,
            "score": s,
            "color": _score_color(s.score),
            "label": _score_label(s.score),
        })

    # Sort
    sort_key = sort if sort in VALID_SORT_COLS else "score"
    reverse = order == "desc"

    if sort_key == "name":
        rows.sort(key=lambda r: (r["person"].name or "").lower(), reverse=reverse)
    elif sort_key == "score":
        rows.sort(key=lambda r: r["score"].score, reverse=reverse)
    elif sort_key == "open_items":
        rows.sort(key=lambda r: r["score"].open_items, reverse=reverse)
    elif sort_key == "blocking_count":
        rows.sort(key=lambda r: r["score"].blocking_count, reverse=reverse)
    elif sort_key == "workstream_count":
        rows.sort(key=lambda r: r["score"].workstream_count, reverse=reverse)
    elif sort_key == "trend":
        trend_order = {"up": 2, "flat": 1, "down": 0}
        rows.sort(
            key=lambda r: trend_order.get(r["score"].trend, 1), reverse=reverse
        )

    return templates.TemplateResponse(
        request,
        "readiness.html",
        {
            "rows": rows,
            "total_people": len(rows),
            "sort": sort_key,
            "order": order,
            "current_time": _current_time(),
        },
    )


@router.get("/readiness/{person_id}/detail", response_class=HTMLResponse)
async def readiness_detail(
    request: Request,
    person_id: int,
    session: AsyncSession = Depends(get_session),
):
    """HTMX partial: expanded detail row showing item breakdown."""
    person = await session.get(Person, person_id)
    if not person:
        return HTMLResponse(
            '<tr><td colspan="6" class="px-4 py-2 text-sm text-red-600">Person not found</td></tr>'
        )

    detail = await get_readiness_detail(session, person_id)

    return templates.TemplateResponse(
        request,
        "components/readiness_detail.html",
        {
            "person": person,
            "detail": detail,
        },
    )
