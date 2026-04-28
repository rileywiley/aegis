"""Asks routes — combined email_asks + chat_asks with status management."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.repositories import (
    get_all_asks,
    get_persons_by_ids,
    update_chat_ask_status,
    update_email_ask_status,
)
from aegis.db.models import ChatAsk, ChatMessage, Email, EmailAsk, Person
from aegis.web import templates
from sqlalchemy import select

router = APIRouter(prefix="/asks")
settings = get_settings()

_STATUS_OPTIONS = ["open", "in_progress", "completed", "stale"]
_STATUS_COLORS = {
    "open": "bg-yellow-50 text-yellow-700",
    "in_progress": "bg-blue-50 text-blue-700",
    "completed": "bg-green-50 text-green-700",
    "stale": "bg-red-50 text-red-600",
}
_STATUS_LABELS = {
    "open": "Open",
    "in_progress": "In Progress",
    "completed": "Completed",
    "stale": "Stale",
}


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


async def _get_user_person_id(session: AsyncSession) -> int | None:
    """Look up the user's person_id from their configured email or name match."""
    user_email = settings.user_email
    if not user_email:
        return None
    # Try exact match first (case-insensitive)
    stmt = select(Person.id).where(func.lower(Person.email) == user_email.lower())
    result = await session.execute(stmt)
    pid = result.scalar_one_or_none()
    if pid:
        return pid
    # Try matching by name parts from email (e.g., "delemos" → "Ricky.Delemos@...")
    name_part = user_email.split("@")[0].replace(".", " ").lower()
    for part in name_part.split():
        if len(part) > 4:
            stmt = select(Person.id).where(Person.email.ilike(f"%{part}%")).limit(1)
            result = await session.execute(stmt)
            pid = result.scalar_one_or_none()
            if pid:
                return pid
    return None


@router.get("")
async def asks_list(
    request: Request,
    tab: str = Query("all", description="Tab: all, inbound, or outbound"),
    status: str = Query("", description="Filter by status"),
    urgency: str = Query("", description="Filter by urgency"),
    ask_type: str = Query("", description="Filter by ask type"),
    source: str = Query("", description="Filter by source: all, email, chat"),
    scope: str = Query("", description="Filter by scope: all, internal, external"),
    page: int = Query(1, ge=1),
    session: AsyncSession = Depends(get_session),
):
    per_page = 25
    tz = _local_tz()

    # Resolve user person_id for directionality filtering
    user_person_id = await _get_user_person_id(session)

    effective_status = status or None

    asks, total = await get_all_asks(
        session,
        status=effective_status,
        urgency=urgency or None,
        ask_type=ask_type or None,
        source=source or None,
        page=page,
        per_page=per_page,
    )

    # Apply directionality filter based on tab
    if tab == "inbound" and user_person_id:
        asks = [a for a in asks if a.get("target_id") == user_person_id]
        total = len(asks)
    elif tab == "outbound" and user_person_id:
        asks = [a for a in asks if a.get("requester_id") == user_person_id]
        total = len(asks)

    # Collect person IDs for name lookup
    person_ids = set()
    for ask in asks:
        if ask.get("requester_id"):
            person_ids.add(ask["requester_id"])
        if ask.get("target_id"):
            person_ids.add(ask["target_id"])
    person_map = await get_persons_by_ids(session, list(person_ids)) if person_ids else {}

    # Apply internal/external scope filter
    if scope == "internal":
        asks = [
            a for a in asks
            if not _is_external_ask(a, person_map)
        ]
        total = len(asks)
    elif scope == "external":
        asks = [
            a for a in asks
            if _is_external_ask(a, person_map)
        ]
        total = len(asks)

    total_pages = max(1, (total + per_page - 1) // per_page)
    now_local = datetime.now(tz)

    return templates.TemplateResponse(
        request,
        "asks.html",
        {
            "asks": asks,
            "person_map": person_map,
            "tab": tab,
            "status": status,
            "urgency": urgency,
            "ask_type": ask_type,
            "source": source,
            "scope": scope,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
            "status_options": _STATUS_OPTIONS,
            "status_colors": _STATUS_COLORS,
            "status_labels": _STATUS_LABELS,
        },
    )


def _is_external_ask(ask: dict, person_map: dict) -> bool:
    """Check if an ask involves an external person (requester or target)."""
    for pid_key in ("requester_id", "target_id"):
        pid = ask.get(pid_key)
        if pid and pid in person_map:
            person = person_map[pid]
            if getattr(person, "is_external", False):
                return True
    return False


@router.get("/{source_type}/{ask_id}")
async def ask_detail(
    request: Request,
    source_type: str,
    ask_id: int,
    from_url: str | None = Query(None, alias="from"),
    session: AsyncSession = Depends(get_session),
):
    """Ask detail view with source context. For chat asks, shows surrounding messages."""
    from aegis.web.breadcrumb import resolve_breadcrumb

    back_url, back_label = resolve_breadcrumb(request, from_url, "/asks", "Asks")
    tz = _local_tz()

    ask = None
    source_item = None
    surrounding_messages: list = []

    if source_type == "email":
        ask_obj = await session.get(EmailAsk, ask_id)
        if ask_obj:
            ask = {
                "id": ask_obj.id,
                "source_type": "email",
                "description": ask_obj.description,
                "ask_type": ask_obj.ask_type,
                "urgency": ask_obj.urgency,
                "status": ask_obj.status,
                "requester_id": ask_obj.requester_id,
                "target_id": ask_obj.target_id,
                "deadline": ask_obj.deadline,
            }
            # Get the source email
            source_item = await session.get(Email, ask_obj.email_id)

    elif source_type == "chat":
        ask_obj = await session.get(ChatAsk, ask_id)
        if ask_obj:
            ask = {
                "id": ask_obj.id,
                "source_type": "chat",
                "description": ask_obj.description,
                "ask_type": ask_obj.ask_type,
                "urgency": ask_obj.urgency,
                "status": ask_obj.status,
                "requester_id": ask_obj.requester_id,
                "target_id": ask_obj.target_id,
                "deadline": ask_obj.deadline,
            }
            # Get the source chat message
            source_msg = await session.get(ChatMessage, ask_obj.message_id)
            source_item = source_msg
            if source_msg and source_msg.chat_id:
                # Get 5 messages before and after in the same chat
                before_stmt = (
                    select(ChatMessage, Person.name.label("sender_name"))
                    .outerjoin(Person, ChatMessage.sender_id == Person.id)
                    .where(
                        ChatMessage.chat_id == source_msg.chat_id,
                        ChatMessage.datetime_ < source_msg.datetime_,
                    )
                    .order_by(ChatMessage.datetime_.desc())
                    .limit(5)
                )
                after_stmt = (
                    select(ChatMessage, Person.name.label("sender_name"))
                    .outerjoin(Person, ChatMessage.sender_id == Person.id)
                    .where(
                        ChatMessage.chat_id == source_msg.chat_id,
                        ChatMessage.datetime_ > source_msg.datetime_,
                    )
                    .order_by(ChatMessage.datetime_.asc())
                    .limit(5)
                )
                before_result = await session.execute(before_stmt)
                after_result = await session.execute(after_stmt)

                before_msgs = [
                    {"msg": row[0], "sender_name": row[1] or "Unknown"}
                    for row in reversed(before_result.all())
                ]
                after_msgs = [
                    {"msg": row[0], "sender_name": row[1] or "Unknown"}
                    for row in after_result.all()
                ]

                # Get sender of the source message
                source_sender = None
                if source_msg.sender_id:
                    source_person = await session.get(Person, source_msg.sender_id)
                    source_sender = source_person.name if source_person else "Unknown"

                surrounding_messages = (
                    before_msgs
                    + [{"msg": source_msg, "sender_name": source_sender or "Unknown", "is_source": True}]
                    + after_msgs
                )

    if not ask:
        return HTMLResponse('<div class="p-8 text-center text-red-600">Ask not found</div>', status_code=404)

    # Resolve person names
    person_ids = set()
    if ask.get("requester_id"):
        person_ids.add(ask["requester_id"])
    if ask.get("target_id"):
        person_ids.add(ask["target_id"])
    person_map = await get_persons_by_ids(session, list(person_ids)) if person_ids else {}

    return templates.TemplateResponse(
        request,
        "ask_detail.html",
        {
            "ask": ask,
            "source_item": source_item,
            "surrounding_messages": surrounding_messages,
            "person_map": person_map,
            "back_url": back_url,
            "back_label": back_label,
            "current_time": datetime.now(tz).strftime("%-I:%M %p %Z"),
            "tz": tz,
            "status_colors": _STATUS_COLORS,
            "status_labels": _STATUS_LABELS,
        },
    )


@router.post("/{source_type}/{ask_id}/status")
async def update_ask_status(
    request: Request,
    source_type: str,
    ask_id: int,
    new_status: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Update ask status via HTMX. source_type is 'email' or 'chat'."""
    valid_statuses = {"open", "in_progress", "completed", "stale"}
    if new_status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")

    if source_type == "email":
        await update_email_ask_status(session, ask_id, new_status)
    elif source_type == "chat":
        await update_chat_ask_status(session, ask_id, new_status)
    else:
        raise HTTPException(status_code=400, detail=f"Invalid source type: {source_type}")

    # Return click-to-cycle badge (matching actions page pattern)
    current_idx = _STATUS_OPTIONS.index(new_status)
    next_status = _STATUS_OPTIONS[(current_idx + 1) % len(_STATUS_OPTIONS)]

    color = _STATUS_COLORS.get(new_status, "bg-gray-100 text-gray-600")
    label = _STATUS_LABELS.get(new_status, new_status)

    html = (
        f'<form hx-post="/asks/{source_type}/{ask_id}/status" hx-swap="outerHTML" hx-target="this">'
        f'<input type="hidden" name="new_status" value="{next_status}">'
        f'<button type="submit" '
        f'class="inline-flex items-center rounded-full px-2 py-1 text-xs font-medium cursor-pointer '
        f'transition-colors hover:opacity-80 {color}">'
        f'{label}</button></form>'
    )
    return HTMLResponse(html)
