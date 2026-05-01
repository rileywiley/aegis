"""Workstreams routes — list, detail, create, status update."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from sqlalchemy import select, text

from aegis.db.models import Email, Meeting, WorkstreamItem
from aegis.db.repositories import (
    create_workstream,
    get_workstream_by_id,
    get_workstream_item_counts,
    get_workstream_items,
    get_workstream_milestones,
    get_workstream_owner_names,
    get_workstream_stakeholders,
    get_workstreams,
    update_workstream,
)
from aegis.web import templates
from aegis.web.breadcrumb import resolve_breadcrumb

router = APIRouter(prefix="/workstreams")
settings = get_settings()

_STATUS_OPTIONS = ["active", "quiet", "paused", "completed", "archived"]


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


def _current_time() -> str:
    tz = _local_tz()
    return datetime.now(tz).strftime("%-I:%M %p %Z")


# ── Item type display helpers ─────────────────────────────

_ITEM_TYPE_LABELS = {
    "meeting": "Meeting",
    "email": "Email",
    "chat_message": "Chat",
    "action_item": "Action Item",
    "decision": "Decision",
    "commitment": "Commitment",
    "email_ask": "Email Ask",
    "chat_ask": "Chat Ask",
}

_ITEM_TYPE_COLORS = {
    "meeting": "bg-blue-50 text-blue-700",
    "email": "bg-purple-50 text-purple-700",
    "chat_message": "bg-cyan-50 text-cyan-700",
    "action_item": "bg-amber-50 text-amber-700",
    "decision": "bg-green-50 text-green-700",
    "commitment": "bg-rose-50 text-rose-700",
    "email_ask": "bg-orange-50 text-orange-700",
    "chat_ask": "bg-teal-50 text-teal-700",
}


async def _generate_workstream_summary(
    ws, items: list, item_details: dict[str, dict[int, dict]],
) -> str:
    """Generate a narrative status update for a workstream using Haiku."""
    import logging
    logger = logging.getLogger("aegis")

    # Build context from the most recent items (limit to keep prompt small)
    context_lines = [f"Workstream: {ws.name}"]
    if ws.description:
        context_lines.append(f"Description: {ws.description}")
    context_lines.append(f"Status: {ws.status}")
    context_lines.append("")

    count = 0
    for item in items[:25]:  # Most recent 25 items
        detail = item_details.get(item.item_type, {}).get(item.item_id, {})
        if not detail:
            continue
        label = _ITEM_TYPE_LABELS.get(item.item_type, item.item_type)
        title = detail.get("title") or ""
        summary = detail.get("summary") or ""
        status = detail.get("status") or ""
        text = f"- [{label}] {title} {summary}".strip()[:150]
        if status:
            text += f" (status: {status})"
        context_lines.append(text)
        count += 1

    if count == 0:
        return ""

    context = "\n".join(context_lines)

    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            temperature=0.3,
            messages=[{"role": "user", "content": f"""\
Based on the following workstream data, write a 2-3 sentence status update as if you were giving a brief verbal update in a meeting. Focus on: what's happening, what's pending, and any blockers or decisions needed. Be specific and concise. Do not use bullet points.

{context}"""}],
        )
        return response.content[0].text.strip()
    except Exception:
        logger.exception("Failed to generate workstream summary for '%s'", ws.name)
        return ""


@router.get("")
async def workstreams_list(
    request: Request,
    q: str = Query("", description="Search by name"),
    status: str = Query("", description="Filter by status"),
    session: AsyncSession = Depends(get_session),
):
    # Default to "active" filter when no status specified
    effective_status = status if status else "active"
    workstream_list = await get_workstreams(
        session,
        status_filter=effective_status if effective_status != "all" else None,
        search=q if q else None,
    )

    # Get item counts and owner names in bulk
    ws_ids = [ws.id for ws in workstream_list]
    item_counts = await get_workstream_item_counts(session, ws_ids)

    owner_ids = [ws.owner_id for ws in workstream_list if ws.owner_id]
    owner_names = await get_workstream_owner_names(session, owner_ids)

    return templates.TemplateResponse(
        request,
        "workstreams.html",
        {
            "workstreams": workstream_list,
            "item_counts": item_counts,
            "owner_names": owner_names,
            "q": q,
            "status_filter": effective_status,
            "status_options": _STATUS_OPTIONS,
            "current_time": _current_time(),
        },
    )


@router.get("/unassigned")
async def unassigned_items(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Show items not assigned to any workstream."""
    # Get IDs of meetings already in a workstream
    assigned_meeting_ids = select(WorkstreamItem.item_id).where(
        WorkstreamItem.item_type == "meeting"
    )
    assigned_email_ids = select(WorkstreamItem.item_id).where(
        WorkstreamItem.item_type == "email"
    )

    # Unassigned meetings (completed processing, not in any workstream)
    meeting_stmt = (
        select(Meeting)
        .where(
            Meeting.processing_status == "completed",
            Meeting.id.not_in(assigned_meeting_ids),
        )
        .order_by(Meeting.start_time.desc())
        .limit(50)
    )
    meeting_result = await session.execute(meeting_stmt)
    unassigned_meetings = list(meeting_result.scalars().all())

    # Unassigned emails
    email_stmt = (
        select(Email)
        .where(
            Email.processing_status == "completed",
            Email.id.not_in(assigned_email_ids),
        )
        .order_by(Email.datetime_.desc())
        .limit(50)
    )
    email_result = await session.execute(email_stmt)
    unassigned_emails = list(email_result.scalars().all())

    # Get workstreams for manual assignment
    ws_list = await get_workstreams(session, status_filter="active")

    tz = _local_tz()

    return templates.TemplateResponse(
        request,
        "unassigned_items.html",
        {
            "meetings": unassigned_meetings,
            "emails": unassigned_emails,
            "workstreams": ws_list,
            "total": len(unassigned_meetings) + len(unassigned_emails),
            "current_time": _current_time(),
            "tz": tz,
        },
    )


@router.get("/{workstream_id}")
async def workstream_detail(
    request: Request,
    workstream_id: int,
    from_url: str | None = Query(None, alias="from"),
    session: AsyncSession = Depends(get_session),
):
    ws = await get_workstream_by_id(session, workstream_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workstream not found")

    items = await get_workstream_items(session, workstream_id)
    stakeholders = await get_workstream_stakeholders(session, workstream_id)
    milestones = await get_workstream_milestones(session, workstream_id)

    back_url, back_label = resolve_breadcrumb(request, from_url, "/workstreams", "Workstreams")

    # Resolve owner name
    owner_name = None
    if ws.owner_id:
        names = await get_workstream_owner_names(session, [ws.owner_id])
        owner_name = names.get(ws.owner_id)

    # Resolve item content for timeline display
    from aegis.db.models import (
        ActionItem, ChatAsk, ChatMessage, Decision, Commitment, Email, EmailAsk,
    )
    item_details: dict[str, dict[int, dict]] = {}
    _type_model_map = {
        "meeting": (Meeting, lambda m: {"title": m.title, "summary": m.summary}),
        "email": (Email, lambda e: {"title": e.subject, "summary": e.summary or e.body_preview}),
        "chat_message": (ChatMessage, lambda c: {"title": None, "summary": c.summary or c.body_preview or (c.body_text or "")[:120]}),
        "action_item": (ActionItem, lambda a: {"title": None, "summary": a.description, "status": a.status}),
        "decision": (Decision, lambda d: {"title": None, "summary": d.description}),
        "commitment": (Commitment, lambda c: {"title": None, "summary": c.description}),
        "email_ask": (EmailAsk, lambda a: {"title": None, "summary": a.description, "status": a.status}),
        "chat_ask": (ChatAsk, lambda a: {"title": None, "summary": a.description, "status": a.status}),
    }
    for item in items:
        if item.item_type not in item_details:
            item_details[item.item_type] = {}
        if item.item_type in _type_model_map:
            model_cls, extractor = _type_model_map[item.item_type]
            obj = await session.get(model_cls, item.item_id)
            if obj:
                item_details[item.item_type][item.item_id] = extractor(obj)

    # Generate LLM narrative summary from linked item content
    written_summary = ""
    if items:
        written_summary = await _generate_workstream_summary(ws, items, item_details)

    tz = _local_tz()

    return templates.TemplateResponse(
        request,
        "workstream_detail.html",
        {
            "ws": ws,
            "items": items,
            "item_details": item_details,
            "written_summary": written_summary,
            "stakeholders": stakeholders,
            "milestones": milestones,
            "owner_name": owner_name,
            "back_url": back_url,
            "back_label": back_label,
            "status_options": _STATUS_OPTIONS,
            "item_type_labels": _ITEM_TYPE_LABELS,
            "item_type_colors": _ITEM_TYPE_COLORS,
            "current_time": _current_time(),
            "tz": tz,
        },
    )


@router.post("")
async def create_workstream_route(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    status: str = Form("active"),
    target_date: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    parsed_date: date | None = None
    if target_date:
        try:
            parsed_date = date.fromisoformat(target_date)
        except ValueError:
            parsed_date = None

    ws = await create_workstream(
        session,
        name=name,
        description=description if description else None,
        status=status,
        target_date=parsed_date,
    )

    # If HTMX request, redirect with HX-Redirect header
    if request.headers.get("HX-Request"):
        response = HTMLResponse(status_code=200)
        response.headers["HX-Redirect"] = f"/workstreams/{ws.id}"
        return response

    return RedirectResponse(url=f"/workstreams/{ws.id}", status_code=303)


@router.post("/{workstream_id}/status")
async def update_workstream_status(
    request: Request,
    workstream_id: int,
    new_status: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    ws = await update_workstream(session, workstream_id, status=new_status)
    if not ws:
        raise HTTPException(status_code=404, detail="Workstream not found")

    # Return updated status badge HTML for HTMX swap
    color_map = {
        "active": "bg-green-50 text-green-700",
        "quiet": "bg-gray-100 text-gray-600",
        "paused": "bg-amber-50 text-amber-700",
        "completed": "bg-blue-50 text-blue-700",
        "archived": "bg-gray-100 text-gray-500",
    }
    color = color_map.get(new_status, "bg-gray-100 text-gray-600")
    html = (
        f'<span class="inline-flex items-center rounded-full px-2 py-1 text-xs font-medium {color}">'
        f'{new_status.capitalize()}</span>'
    )
    return HTMLResponse(html)


@router.post("/assign-item")
async def assign_item_to_workstream(
    request: Request,
    workstream_id: int = Form(...),
    item_type: str = Form(...),
    item_id: int = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Manually assign an item to a workstream."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = pg_insert(WorkstreamItem).values(
        workstream_id=workstream_id,
        item_type=item_type,
        item_id=item_id,
        linked_by="manual",
    )
    stmt = stmt.on_conflict_do_nothing(
        constraint="uq_workstream_item"
    )
    await session.execute(stmt)
    await session.commit()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/workstreams/unassigned", status_code=303)


@router.post("/detect")
async def trigger_workstream_detection(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Manually trigger workstream detection (Layer 1 clustering)."""
    import asyncio
    import logging

    logger = logging.getLogger("aegis")

    try:
        from aegis.db.engine import async_session_factory
        from aegis.processing.workstream_detector import run_weekly_clustering

        async def _run():
            async with async_session_factory() as bg_session:
                await run_weekly_clustering(bg_session)

        asyncio.create_task(_run())
        return HTMLResponse(
            '<div class="rounded-lg bg-green-50 border border-green-200 p-3 text-sm text-green-700">'
            'Workstream detection started. Page will refresh in 10 seconds...'
            '</div>'
            '<script>setTimeout(() => window.location.reload(), 10000)</script>'
        )
    except Exception:
        logger.exception("Failed to start workstream detection")
        return HTMLResponse(
            '<div class="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">'
            'Failed to start detection. Check logs.</div>'
        )


@router.post("/{workstream_id}/scan")
async def trigger_workstream_scan(
    request: Request,
    workstream_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Manually trigger item assignment scan for a specific workstream."""
    import asyncio
    import logging

    logger = logging.getLogger("aegis")

    ws = await get_workstream_by_id(session, workstream_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workstream not found")

    try:
        from aegis.db.engine import async_session_factory
        from aegis.processing.workstream_detector import run_workstream_assignment

        async def _run():
            async with async_session_factory() as bg_session:
                await run_workstream_assignment(bg_session)

        asyncio.create_task(_run())
        return HTMLResponse(
            '<div class="rounded-lg bg-green-50 border border-green-200 p-3 text-sm text-green-700">'
            'Item scan started. Page will refresh in 10 seconds...'
            '</div>'
            '<script>setTimeout(() => window.location.reload(), 10000)</script>'
        )
    except Exception:
        logger.exception("Failed to start workstream item scan")
        return HTMLResponse(
            '<div class="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-700">'
            'Failed to start scan. Check logs.</div>'
        )
