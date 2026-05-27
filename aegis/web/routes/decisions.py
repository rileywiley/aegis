"""Decisions page — pending + recent decisions with source tracing."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import ActionItem, Decision, EmailAsk, Person
from aegis.web import templates

router = APIRouter(prefix="/decisions")
settings = get_settings()


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


@router.get("")
async def decisions_list(
    request: Request,
    status: str = Query("", description="Filter by status: pending, resolved"),
    session: AsyncSession = Depends(get_session),
):
    """Decisions page with pending and recent decisions."""
    tz = _local_tz()
    now = datetime.now(tz)

    # Resolve user person_id
    user_email = settings.user_email
    user_id = None
    if user_email:
        result = await session.execute(
            select(Person.id).where(func.lower(Person.email) == user_email.lower())
        )
        user_id = result.scalar_one_or_none()

    # Pending decisions (pending_owner = user or all)
    pending_stmt = (
        select(Decision)
        .where(Decision.status == "pending")
        .order_by(Decision.datetime_.desc())
        .limit(25)
    )
    pending_result = await session.execute(pending_stmt)
    pending_decisions = list(pending_result.scalars().all())

    # Recent resolved decisions (last 30 days)
    cutoff = datetime.now(tz).astimezone(ZoneInfo("UTC")) - timedelta(days=30)
    resolved_stmt = (
        select(Decision)
        .where(Decision.status == "resolved", Decision.datetime_ >= cutoff)
        .order_by(Decision.datetime_.desc())
        .limit(25)
    )
    resolved_result = await session.execute(resolved_stmt)
    resolved_decisions = list(resolved_result.scalars().all())

    # Collect person IDs for name resolution
    person_ids = set()
    for d in pending_decisions + resolved_decisions:
        if d.decided_by:
            person_ids.add(d.decided_by)
        if d.pending_owner_id:
            person_ids.add(d.pending_owner_id)

    person_map: dict[int, str] = {}
    if person_ids:
        from aegis.db.repositories import get_persons_by_ids
        persons = await get_persons_by_ids(session, list(person_ids))
        person_map = {pid: p.name for pid, p in persons.items()}

    # Get related action items for resolved decisions
    decision_actions: dict[int, list] = {}
    all_decision_ids = [d.id for d in resolved_decisions]
    if all_decision_ids:
        ai_stmt = (
            select(ActionItem)
            .where(ActionItem.related_decision_id.in_(all_decision_ids))
            .order_by(ActionItem.created.desc())
        )
        ai_result = await session.execute(ai_stmt)
        for ai in ai_result.scalars().all():
            decision_actions.setdefault(ai.related_decision_id, []).append(ai)

    return templates.TemplateResponse(
        request,
        "decisions.html",
        {
            "pending_decisions": pending_decisions,
            "resolved_decisions": resolved_decisions,
            "person_map": person_map,
            "decision_actions": decision_actions,
            "current_time": now.strftime("%-I:%M %p %Z"),
            "tz": tz,
        },
    )
