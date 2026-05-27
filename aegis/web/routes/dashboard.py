"""Dashboard — Command Center route with all 6 zones."""

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
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
    SystemHealth,
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
    """Zone 1: Active workstreams, pinned first, with sentiment."""
    from aegis.db.models import SentimentAggregation

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

        # Sentiment for this workstream
        sentiment_stmt = (
            select(SentimentAggregation)
            .where(
                SentimentAggregation.scope_type == "workstream",
                SentimentAggregation.scope_id == str(ws.id),
            )
            .order_by(SentimentAggregation.period_end.desc())
            .limit(1)
        )
        sentiment_result = await session.execute(sentiment_stmt)
        sentiment_row = sentiment_result.scalar_one_or_none()

        card = {
            "id": ws.id,
            "name": ws.name,
            "status": ws.status,
            "pinned": ws.pinned,
            "item_count": item_count,
            "last_activity": last_activity.isoformat() if last_activity else None,
            "description": (ws.description or "")[:100],
            "sentiment_score": sentiment_row.avg_score if sentiment_row else None,
            "sentiment_trend": sentiment_row.trend if sentiment_row else None,
        }
        cards.append(card)

    return {"cards": cards}


async def _get_user_person_id(session: AsyncSession) -> int | None:
    """Look up the user's person_id from their configured email."""
    user_email = settings.user_email
    if not user_email:
        return None
    stmt = select(Person.id).where(func.lower(Person.email) == user_email.lower())
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _compute_needs_your_action(session: AsyncSession) -> dict:
    """Zone 2 tab: Items where the user is the bottleneck."""
    user_id = await _get_user_person_id(session)
    items = []
    stale_days = settings.stale_action_item_days

    if user_id:
        # Pending decisions where user needs to decide
        dec_stmt = (
            select(Decision)
            .where(Decision.pending_owner_id == user_id, Decision.status == "pending")
            .order_by(Decision.datetime_.desc())
            .limit(10)
        )
        dec_result = await session.execute(dec_stmt)
        for d in dec_result.scalars().all():
            items.append({
                "item_type": "decision", "id": d.id,
                "description": d.description,
                "from_person_id": None,
                "urgency": "high",
                "created": d.datetime_.isoformat() if d.datetime_ else None,
                "source_meeting_id": d.source_meeting_id,
                "source_email_id": d.source_email_id,
            })

        # Asks directed at the user
        ea_stmt = (
            select(EmailAsk)
            .where(EmailAsk.target_id == user_id, EmailAsk.status == "open")
            .order_by(EmailAsk.created.desc())
            .limit(10)
        )
        for ea in (await session.execute(ea_stmt)).scalars().all():
            items.append({
                "item_type": "ask", "id": ea.id,
                "description": ea.description,
                "from_person_id": ea.requester_id,
                "urgency": ea.urgency,
                "created": ea.created.isoformat() if ea.created else None,
                "source": "email", "source_id": ea.email_id,
            })
        ca_stmt = (
            select(ChatAsk)
            .where(ChatAsk.target_id == user_id, ChatAsk.status == "open")
            .order_by(ChatAsk.created.desc())
            .limit(10)
        )
        for ca in (await session.execute(ca_stmt)).scalars().all():
            items.append({
                "item_type": "ask", "id": ca.id,
                "description": ca.description,
                "from_person_id": ca.requester_id,
                "urgency": ca.urgency,
                "created": ca.created.isoformat() if ca.created else None,
                "source": "chat", "source_id": ca.message_id,
            })

        # Action items assigned to the user
        ai_stmt = (
            select(ActionItem)
            .where(ActionItem.assignee_id == user_id, ActionItem.status.in_(["open", "in_progress"]))
            .order_by(ActionItem.created.asc())
            .limit(10)
        )
        threshold = datetime.now(timezone.utc) - timedelta(days=stale_days)
        for ai in (await session.execute(ai_stmt)).scalars().all():
            urg = "high" if ai.created and ai.created <= threshold else "medium"
            items.append({
                "item_type": "action", "id": ai.id,
                "description": ai.description,
                "from_person_id": None,
                "urgency": urg,
                "created": ai.created.isoformat() if ai.created else None,
                "source_meeting_id": ai.source_meeting_id,
            })

    # Sort: high urgency first, then oldest first
    urgency_order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda x: (urgency_order.get(x.get("urgency", "low"), 2), x.get("created") or ""))
    return {"items": items[:25], "count": len(items)}


async def _compute_awaiting_others(session: AsyncSession) -> dict:
    """Zone 2 tab: Items where others owe the user."""
    user_id = await _get_user_person_id(session)
    items = []
    stale_days = settings.stale_action_item_days

    if user_id:
        # Asks the user made to others
        ea_stmt = (
            select(EmailAsk)
            .where(EmailAsk.requester_id == user_id, EmailAsk.status == "open")
            .order_by(EmailAsk.created.desc())
            .limit(15)
        )
        for ea in (await session.execute(ea_stmt)).scalars().all():
            items.append({
                "item_type": "ask", "id": ea.id,
                "description": ea.description,
                "who_owes_id": ea.target_id,
                "urgency": ea.urgency,
                "created": ea.created.isoformat() if ea.created else None,
                "source": "email", "source_id": ea.email_id,
            })
        ca_stmt = (
            select(ChatAsk)
            .where(ChatAsk.requester_id == user_id, ChatAsk.status == "open")
            .order_by(ChatAsk.created.desc())
            .limit(15)
        )
        for ca in (await session.execute(ca_stmt)).scalars().all():
            items.append({
                "item_type": "ask", "id": ca.id,
                "description": ca.description,
                "who_owes_id": ca.target_id,
                "urgency": ca.urgency,
                "created": ca.created.isoformat() if ca.created else None,
                "source": "chat", "source_id": ca.message_id,
            })

        # Action items assigned to others from meetings user attended
        from aegis.db.models import MeetingAttendee
        user_meeting_ids = select(MeetingAttendee.meeting_id).where(
            MeetingAttendee.person_id == user_id
        )
        ai_stmt = (
            select(ActionItem)
            .where(
                ActionItem.assignee_id != user_id,
                ActionItem.assignee_id.is_not(None),
                ActionItem.status.in_(["open", "in_progress"]),
                ActionItem.source_meeting_id.in_(user_meeting_ids),
            )
            .order_by(ActionItem.created.asc())
            .limit(15)
        )
        threshold = datetime.now(timezone.utc) - timedelta(days=stale_days)
        for ai in (await session.execute(ai_stmt)).scalars().all():
            urg = "high" if ai.created and ai.created <= threshold else "medium"
            items.append({
                "item_type": "action", "id": ai.id,
                "description": ai.description,
                "who_owes_id": ai.assignee_id,
                "urgency": urg,
                "created": ai.created.isoformat() if ai.created else None,
            })

    urgency_order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda x: (urgency_order.get(x.get("urgency", "low"), 2), x.get("created") or ""))
    return {"items": items[:25], "count": len(items)}


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

    # Daily briefing — only show today's briefing, not stale ones from previous days
    start_utc, end_utc = _today_range_utc()
    briefing_stmt = (
        select(Briefing)
        .where(
            Briefing.briefing_type.in_(["morning", "monday"]),
            Briefing.generated_at >= start_utc,
            Briefing.generated_at < end_utc,
        )
        .order_by(Briefing.generated_at.desc())
        .limit(1)
    )
    briefing_result = await session.execute(briefing_stmt)
    daily_briefing = briefing_result.scalar_one_or_none()

    # Zone 1: Workstream cards (from cache)
    ws_data = await _get_cached_or_compute(session, "workstream_cards", _compute_workstream_cards)
    workstream_cards = ws_data.get("cards", []) if isinstance(ws_data, dict) else []

    # Zone 2: Requires attention tabs (from cache)
    needs_action_data = await _get_cached_or_compute(session, "needs_your_action", _compute_needs_your_action)
    awaiting_others_data = await _get_cached_or_compute(session, "awaiting_others", _compute_awaiting_others)

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

    # Resolve person names for all dashboard items
    draft_items = drafts_data.get("items", []) if isinstance(drafts_data, dict) else []
    needs_action_items = needs_action_data.get("items", []) if isinstance(needs_action_data, dict) else []
    awaiting_others_items = awaiting_others_data.get("items", []) if isinstance(awaiting_others_data, dict) else []

    person_ids: set[int] = set()
    for d in draft_items:
        if d.get("recipient_id"):
            person_ids.add(d["recipient_id"])
    for item in needs_action_items:
        if item.get("from_person_id"):
            person_ids.add(item["from_person_id"])
    for item in awaiting_others_items:
        if item.get("who_owes_id"):
            person_ids.add(item["who_owes_id"])

    person_names: dict[int, str] = {}
    if person_ids:
        from aegis.db.repositories import get_persons_by_ids
        persons = await get_persons_by_ids(session, list(person_ids))
        person_names = {pid: p.name for pid, p in persons.items()}

    # Today's voice notes — mix into the daily timeline alongside meetings
    from aegis.db.voice_notes_repository import VoiceNotesRepository
    voice_notes_repo = VoiceNotesRepository(session)
    todays_voice_notes = await voice_notes_repo.list_in_range(start_utc, end_utc)

    # Last sync: most recent last_success from system_health
    last_sync_stmt = select(func.max(SystemHealth.last_success))
    last_sync_result = await session.execute(last_sync_stmt)
    last_sync_time = last_sync_result.scalar_one_or_none()
    last_sync_minutes_ago = None
    if last_sync_time:
        last_sync_tz = last_sync_time.replace(tzinfo=timezone.utc) if last_sync_time.tzinfo is None else last_sync_time
        last_sync_minutes_ago = int((now_utc - last_sync_tz).total_seconds() / 60)

    # Service health alerts — show degraded/down services
    health_stmt = select(SystemHealth).where(SystemHealth.status.in_(["degraded", "down"]))
    health_result = await session.execute(health_stmt)
    unhealthy_services = list(health_result.scalars().all())

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "meetings": meetings,
            "meeting_briefs": meeting_briefs,
            "today_str": now_local.strftime("%A, %B %-d, %Y"),
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
            # Daily briefing
            "daily_briefing": daily_briefing,
            # Zone 1
            "workstream_cards": workstream_cards,
            # Zone 2
            "needs_action": needs_action_data.get("items", []) if isinstance(needs_action_data, dict) else [],
            "needs_action_count": needs_action_data.get("count", 0) if isinstance(needs_action_data, dict) else 0,
            "awaiting_others": awaiting_others_data.get("items", []) if isinstance(awaiting_others_data, dict) else [],
            "awaiting_others_count": awaiting_others_data.get("count", 0) if isinstance(awaiting_others_data, dict) else 0,
            # Zone 4
            "drafts": draft_items,
            "drafts_count": drafts_data.get("count", 0) if isinstance(drafts_data, dict) else 0,
            "person_names": person_names,
            # Zone 5
            "next_meeting": next_meeting,
            # Voice notes for the daily timeline
            "todays_voice_notes": todays_voice_notes,
            # Last sync
            "last_sync_minutes_ago": last_sync_minutes_ago,
            # Service health alerts
            "unhealthy_services": unhealthy_services,
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
    # Invalidate drafts cache
    from sqlalchemy import delete
    await session.execute(delete(DashboardCache).where(DashboardCache.key == "drafts_pending"))
    await session.commit()
    return HTMLResponse(
        '<li class="px-6 py-3 text-sm text-green-600">Sent</li>'
    )


@router.post("/api/drafts/{draft_id}/discard")
async def discard_draft(draft_id: int, session: AsyncSession = Depends(get_session)):
    """Discard a draft."""
    stmt = (
        update(Draft)
        .where(Draft.id == draft_id)
        .values(status="discarded")
    )
    await session.execute(stmt)
    # Invalidate drafts cache
    from sqlalchemy import delete
    await session.execute(delete(DashboardCache).where(DashboardCache.key == "drafts_pending"))
    await session.commit()
    return HTMLResponse(
        '<li class="px-6 py-3 text-sm text-gray-400">Discarded</li>'
    )


async def invalidate_dashboard_cache(
    session: AsyncSession,
    keys: list[str] | None = None,
) -> None:
    """Delete dashboard cache entries so next page load recomputes fresh data."""
    if keys:
        from sqlalchemy import delete
        stmt = delete(DashboardCache).where(DashboardCache.key.in_(keys))
    else:
        from sqlalchemy import delete
        stmt = delete(DashboardCache)
    await session.execute(stmt)
    await session.commit()


@router.post("/api/decisions/{decision_id}/resolve")
async def resolve_decision(
    decision_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Toggle a decision's status between open and resolved."""
    decision = await session.get(Decision, decision_id)
    if not decision:
        return HTMLResponse('<span class="text-red-600 text-xs">Not found</span>')

    new_status = "resolved" if decision.status != "resolved" else "open"
    decision.status = new_status
    # Invalidate cache in same transaction
    from sqlalchemy import delete
    await session.execute(delete(DashboardCache).where(DashboardCache.key == "needs_your_action"))
    await session.commit()

    if new_status == "resolved":
        return HTMLResponse(
            '<li class="px-6 py-3 text-sm text-green-600">Resolved</li>'
        )
    else:
        return HTMLResponse(
            f'<li class="px-6 py-3 text-sm text-yellow-600">Reopened — will reappear on refresh</li>'
        )


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

    async def _compute_todays_meetings(session):
        start_utc, end_utc = _today_range_utc()
        meetings = await get_meetings_for_range(session, start_utc, end_utc)
        return {"items": [{"id": m.id, "title": m.title, "start_time": m.start_time.isoformat(), "status": m.status} for m in meetings]}

    async def _compute_readiness_scores(session):
        try:
            from aegis.intelligence.readiness import compute_all_readiness
            scores = await compute_all_readiness(session)
            return {"scores": [s.model_dump() for s in scores]}
        except Exception:
            return {"scores": []}

    async def _compute_department_health(session):
        from aegis.db.models import Department
        result = await session.execute(select(Department).limit(20))
        depts = list(result.scalars().all())
        return {"departments": [{"id": d.id, "name": d.name} for d in depts]}

    async with async_session_factory() as session:
        for key, fn in [
            ("workstream_cards", _compute_workstream_cards),
            ("needs_your_action", _compute_needs_your_action),
            ("awaiting_others", _compute_awaiting_others),
            ("drafts_pending", _compute_drafts_pending),
            ("todays_meetings", _compute_todays_meetings),
            ("readiness_scores", _compute_readiness_scores),
            ("department_health", _compute_department_health),
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

    # Also refresh nav badge counts
    try:
        from aegis.web.nav_counts import get_nav_counts
        await get_nav_counts()
    except Exception:
        pass

    logger.info("Dashboard cache refreshed")
