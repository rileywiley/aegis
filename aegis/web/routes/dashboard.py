"""Dashboard — Command Center route with all 6 zones."""

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import (
    ActionItem,
    Briefing,
    ChatAsk,
    DashboardCache,
    Decision,
    Draft,
    Email,
    EmailAsk,
    Meeting,
    Person,
    Workstream,
    WorkstreamItem,
)
from aegis.db.repositories import get_meetings_for_range
from aegis.web import templates

logger = logging.getLogger(__name__)

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


async def _get_cached_or_compute(
    session: AsyncSession, key: str, compute_fn, ttl_seconds: int | None = None,
) -> dict | list:
    """Read from dashboard_cache if fresh, otherwise compute and store."""
    if ttl_seconds is None:
        ttl_seconds = settings.dashboard_cache_ttl_seconds

    stmt = select(DashboardCache).where(DashboardCache.key == key)
    result = await session.execute(stmt)
    cached = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if cached and cached.computed_at:
        age = (now - cached.computed_at.replace(tzinfo=timezone.utc if cached.computed_at.tzinfo is None else cached.computed_at.tzinfo)).total_seconds()
        if age < ttl_seconds:
            return cached.data

    # Compute fresh data
    data = await compute_fn(session)

    # Upsert to cache
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    stmt = pg_insert(DashboardCache).values(
        key=key, data=data if isinstance(data, dict) else {"items": data},
        computed_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"data": data if isinstance(data, dict) else {"items": data}, "computed_at": now},
    )
    await session.execute(stmt)
    await session.commit()

    return data if isinstance(data, dict) else {"items": data}


# ── Zone compute functions ────────────────────────────────


async def _compute_workstream_cards(session: AsyncSession) -> dict:
    """Zone 1: Active workstreams, pinned first."""
    max_slots = settings.dashboard_max_workstream_slots
    stmt = (
        select(Workstream)
        .where(Workstream.status == "active")
        .order_by(Workstream.pinned.desc(), Workstream.updated.desc())
        .limit(max_slots)
    )
    result = await session.execute(stmt)
    workstreams = list(result.scalars().all())

    cards = []
    for ws in workstreams:
        # Count items
        item_count_stmt = select(func.count()).select_from(WorkstreamItem).where(
            WorkstreamItem.workstream_id == ws.id
        )
        item_count = (await session.execute(item_count_stmt)).scalar_one()

        # Last activity: most recent linked_at
        last_stmt = (
            select(func.max(WorkstreamItem.linked_at))
            .where(WorkstreamItem.workstream_id == ws.id)
        )
        last_activity = (await session.execute(last_stmt)).scalar_one()

        cards.append({
            "id": ws.id,
            "name": ws.name,
            "status": ws.status,
            "pinned": ws.pinned,
            "item_count": item_count,
            "last_activity": last_activity.isoformat() if last_activity else None,
            "description": (ws.description or "")[:100],
        })

    return {"cards": cards}


async def _compute_pending_decisions(session: AsyncSession) -> dict:
    """Zone 2 tab: Unresolved decisions from recent meetings."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    stmt = (
        select(Decision)
        .where(Decision.datetime_ >= cutoff)
        .order_by(Decision.datetime_.desc())
        .limit(20)
    )
    result = await session.execute(stmt)
    decisions = list(result.scalars().all())

    items = []
    for d in decisions:
        items.append({
            "id": d.id,
            "description": d.description,
            "datetime": d.datetime_.isoformat() if d.datetime_ else None,
            "source_meeting_id": d.source_meeting_id,
            "source_email_id": d.source_email_id,
        })
    return {"items": items, "count": len(items)}


async def _compute_awaiting_response(session: AsyncSession) -> dict:
    """Zone 2 tab: Open asks where user is the target."""
    ea_stmt = (
        select(EmailAsk)
        .where(EmailAsk.status == "open")
        .order_by(EmailAsk.created.desc())
        .limit(20)
    )
    ea_result = await session.execute(ea_stmt)
    email_asks = list(ea_result.scalars().all())

    ca_stmt = (
        select(ChatAsk)
        .where(ChatAsk.status == "open")
        .order_by(ChatAsk.created.desc())
        .limit(20)
    )
    ca_result = await session.execute(ca_stmt)
    chat_asks = list(ca_result.scalars().all())

    items = []
    for ea in email_asks:
        items.append({
            "id": ea.id,
            "description": ea.description,
            "ask_type": ea.ask_type,
            "urgency": ea.urgency,
            "created": ea.created.isoformat() if ea.created else None,
            "source": "email",
            "source_id": ea.email_id,
        })
    for ca in chat_asks:
        items.append({
            "id": ca.id,
            "description": ca.description,
            "ask_type": ca.ask_type,
            "urgency": ca.urgency,
            "created": ca.created.isoformat() if ca.created else None,
            "source": "chat",
            "source_id": ca.message_id,
        })

    items.sort(key=lambda x: x.get("created") or "", reverse=True)
    return {"items": items[:20], "count": len(items)}


async def _compute_stale_items(session: AsyncSession) -> dict:
    """Zone 2 tab: Stale action items past threshold."""
    threshold = datetime.now(timezone.utc) - timedelta(days=settings.stale_action_item_days)
    stmt = (
        select(ActionItem)
        .where(
            ActionItem.status.in_(["open", "in_progress"]),
            ActionItem.created <= threshold,
        )
        .order_by(ActionItem.created.asc())
        .limit(20)
    )
    result = await session.execute(stmt)
    items_list = list(result.scalars().all())

    items = []
    for ai in items_list:
        items.append({
            "id": ai.id,
            "description": ai.description,
            "status": ai.status,
            "created": ai.created.isoformat() if ai.created else None,
            "assignee_id": ai.assignee_id,
            "deadline": ai.deadline,
        })
    return {"items": items, "count": len(items)}


async def _compute_drafts_pending(session: AsyncSession) -> dict:
    """Zone 4: Drafts awaiting review."""
    stmt = (
        select(Draft)
        .where(Draft.status == "pending_review")
        .order_by(Draft.created.desc())
        .limit(10)
    )
    result = await session.execute(stmt)
    drafts = list(result.scalars().all())

    items = []
    for d in drafts:
        items.append({
            "id": d.id,
            "draft_type": d.draft_type,
            "channel": d.channel,
            "subject": d.subject,
            "body_preview": (d.body or "")[:150],
            "recipient_id": d.recipient_id,
            "created": d.created.isoformat() if d.created else None,
        })
    return {"items": items, "count": len(items)}


# ── Main dashboard route ─────────────────────────────────


@router.get("/")
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    start_utc, end_utc = _today_range_utc()
    meetings = await get_meetings_for_range(session, start_utc, end_utc)

    tz = _local_tz()
    now_local = datetime.now(tz)
    now_utc = datetime.now(timezone.utc)

    # Zone 1: Workstream cards (from cache)
    ws_data = await _get_cached_or_compute(session, "workstream_cards", _compute_workstream_cards)
    workstream_cards = ws_data.get("cards", []) if isinstance(ws_data, dict) else []

    # Zone 2: Requires attention tabs (from cache)
    decisions_data = await _get_cached_or_compute(session, "pending_decisions", _compute_pending_decisions)
    awaiting_data = await _get_cached_or_compute(session, "awaiting_response", _compute_awaiting_response)
    stale_data = await _get_cached_or_compute(session, "stale_items", _compute_stale_items)

    # Zone 3: Today's meetings — already have from live query
    # Enhance with prep brief availability
    meeting_briefs: dict[int, bool] = {}
    for m in meetings:
        stmt = select(func.count()).select_from(Briefing).where(
            Briefing.briefing_type == "meeting_prep",
            Briefing.related_meeting_id == m.id,
        )
        count = (await session.execute(stmt)).scalar_one()
        meeting_briefs[m.id] = count > 0

    # Zone 4: Drafts (from cache)
    drafts_data = await _get_cached_or_compute(session, "drafts_pending", _compute_drafts_pending)

    # Zone 5: Next up meeting
    next_meeting = None
    for m in meetings:
        if m.start_time and m.start_time.replace(tzinfo=timezone.utc if m.start_time.tzinfo is None else m.start_time.tzinfo) > now_utc:
            next_meeting = m
            break

    # Resolve person names for drafts
    draft_items = drafts_data.get("items", []) if isinstance(drafts_data, dict) else []
    person_ids = [d["recipient_id"] for d in draft_items if d.get("recipient_id")]
    person_names: dict[int, str] = {}
    if person_ids:
        from aegis.db.repositories import get_persons_by_ids
        persons = await get_persons_by_ids(session, person_ids)
        person_names = {pid: p.name for pid, p in persons.items()}

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "meetings": meetings,
            "meeting_briefs": meeting_briefs,
            "today_str": now_local.strftime("%A, %B %-d, %Y"),
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
            # Zone 1
            "workstream_cards": workstream_cards,
            # Zone 2
            "decisions": decisions_data.get("items", []) if isinstance(decisions_data, dict) else [],
            "decisions_count": decisions_data.get("count", 0) if isinstance(decisions_data, dict) else 0,
            "awaiting": awaiting_data.get("items", []) if isinstance(awaiting_data, dict) else [],
            "awaiting_count": awaiting_data.get("count", 0) if isinstance(awaiting_data, dict) else 0,
            "stale_items": stale_data.get("items", []) if isinstance(stale_data, dict) else [],
            "stale_count": stale_data.get("count", 0) if isinstance(stale_data, dict) else 0,
            # Zone 4
            "drafts": draft_items,
            "drafts_count": drafts_data.get("count", 0) if isinstance(drafts_data, dict) else 0,
            "person_names": person_names,
            # Zone 5
            "next_meeting": next_meeting,
        },
    )


@router.get("/api/meetings-today")
async def meetings_today_partial(request: Request, session: AsyncSession = Depends(get_session)):
    """HTMX partial — returns just the meetings list HTML fragment."""
    start_utc, end_utc = _today_range_utc()
    meetings = await get_meetings_for_range(session, start_utc, end_utc)
    tz = _local_tz()

    meeting_briefs: dict[int, bool] = {}
    for m in meetings:
        stmt = select(func.count()).select_from(Briefing).where(
            Briefing.briefing_type == "meeting_prep",
            Briefing.related_meeting_id == m.id,
        )
        count = (await session.execute(stmt)).scalar_one()
        meeting_briefs[m.id] = count > 0

    return templates.TemplateResponse(
        request,
        "components/meetings_today.html",
        {
            "meetings": meetings,
            "meeting_briefs": meeting_briefs,
            "tz": tz,
        },
    )


@router.post("/api/drafts/{draft_id}/send")
async def send_draft(draft_id: int, session: AsyncSession = Depends(get_session)):
    """Mark a draft as sent (actual sending is handled by response workflow)."""
    stmt = (
        update(Draft)
        .where(Draft.id == draft_id)
        .values(status="sent", sent_at=datetime.now(timezone.utc))
    )
    await session.execute(stmt)
    await session.commit()
    return {"status": "sent"}


@router.post("/api/drafts/{draft_id}/discard")
async def discard_draft(draft_id: int, session: AsyncSession = Depends(get_session)):
    """Discard a draft."""
    stmt = (
        update(Draft)
        .where(Draft.id == draft_id)
        .values(status="discarded")
    )
    await session.execute(stmt)
    await session.commit()
    return {"status": "discarded"}


@router.get("/api/chat-widget")
async def chat_widget_submit(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
):
    """Lightweight chat widget endpoint for the dashboard sidebar."""
    if not q.strip():
        return templates.TemplateResponse(
            request,
            "components/chat_widget_response.html",
            {"answer": "", "sources": []},
        )

    from aegis.chat.rag import ask_aegis
    result = await ask_aegis(session, q.strip())
    return templates.TemplateResponse(
        request,
        "components/chat_widget_response.html",
        {
            "answer": result["answer"],
            "sources": result["sources"],
        },
    )


# ── Cache refresh function (called by scheduler) ─────────


async def refresh_dashboard_cache() -> None:
    """Refresh all dashboard cache keys. Called by APScheduler every 15 min."""
    from aegis.db.engine import async_session_factory

    async with async_session_factory() as session:
        for key, fn in [
            ("workstream_cards", _compute_workstream_cards),
            ("pending_decisions", _compute_pending_decisions),
            ("awaiting_response", _compute_awaiting_response),
            ("stale_items", _compute_stale_items),
            ("drafts_pending", _compute_drafts_pending),
        ]:
            try:
                data = await fn(session)
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                now = datetime.now(timezone.utc)
                stmt = pg_insert(DashboardCache).values(
                    key=key, data=data, computed_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"data": data, "computed_at": now},
                )
                await session.execute(stmt)
                await session.commit()
            except Exception:
                logger.exception("Failed to refresh dashboard cache key: %s", key)
                await session.rollback()

    logger.info("Dashboard cache refreshed")
