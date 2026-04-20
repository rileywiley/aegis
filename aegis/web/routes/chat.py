"""Ask Aegis — RAG chat routes."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.chat.rag import ask_aegis
from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import ChatSession
from aegis.web import templates

router = APIRouter(prefix="/ask", tags=["chat"])
settings = get_settings()


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


@router.get("")
async def chat_page(
    request: Request,
    session_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Full chat page with optional session restore."""
    tz = _local_tz()
    now_local = datetime.now(tz)

    messages: list[dict] = []
    current_session_id = session_id

    if session_id:
        chat_session = await session.get(ChatSession, session_id)
        if chat_session:
            messages = chat_session.messages if isinstance(chat_session.messages, list) else []
            current_session_id = chat_session.id

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "messages": messages,
            "session_id": current_session_id,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
        },
    )


@router.post("")
async def chat_submit(
    request: Request,
    question: str = Form(...),
    session_id: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
):
    """Handle a chat question via HTMX. Returns HTML fragment for the new messages."""
    # Convert session_id from form string to int or None
    parsed_session_id: int | None = None
    if session_id and session_id.strip():
        try:
            parsed_session_id = int(session_id)
        except (ValueError, TypeError):
            parsed_session_id = None

    try:
        result = await ask_aegis(session, question, parsed_session_id)
        answer = result.get("answer", "")
        sources = result.get("sources", [])
        new_session_id = result.get("session_id", parsed_session_id)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("ask_aegis failed")
        answer = f"Sorry, I encountered an error processing your question. Please try again. ({type(e).__name__})"
        sources = []
        new_session_id = parsed_session_id

    return templates.TemplateResponse(
        request,
        "components/chat_messages.html",
        {
            "question": question,
            "answer": answer,
            "sources": sources,
            "session_id": new_session_id,
        },
    )


@router.get("/sessions")
async def chat_sessions_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """List past chat sessions."""
    tz = _local_tz()
    now_local = datetime.now(tz)

    stmt = (
        select(ChatSession)
        .order_by(ChatSession.last_active.desc())
        .limit(50)
    )
    result = await session.execute(stmt)
    sessions = list(result.scalars().all())

    # Extract first user message as preview for each session
    session_previews = []
    for s in sessions:
        msgs = s.messages if isinstance(s.messages, list) else []
        first_q = ""
        for m in msgs:
            if m.get("role") == "user":
                first_q = m.get("content", "")[:80]
                break
        session_previews.append({
            "id": s.id,
            "preview": first_q or "Empty conversation",
            "last_active": s.last_active,
            "message_count": len([m for m in msgs if m.get("role") == "user"]),
        })

    return templates.TemplateResponse(
        request,
        "components/chat_sessions.html",
        {
            "sessions": session_previews,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
            "tz": tz,
        },
    )


@router.get("/session/{chat_session_id}")
async def chat_session_detail(
    request: Request,
    chat_session_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Load a specific chat session — redirects to chat page with session_id."""
    from starlette.responses import RedirectResponse
    return RedirectResponse(url=f"/ask?session_id={chat_session_id}", status_code=302)
