"""Voice-note endpoints — API + dashboard pages.

The original Wave 3K module shipped the JSON API that the Helios menu bar
consumes (``/api/voice-notes/...``). This module extends that contract
with the dashboard surface area introduced in §6E.3-§6E.9:

UI pages:
    GET  /voice-notes              -- list page (filterable)
    GET  /voice-notes/{id}         -- detail page (transcript edit, attachments)
    GET  /voice-notes/{id}/status  -- HTMX poll fragment for the status pill

Transcript / attachment / re-extract endpoints used by the detail page:
    PATCH /voice-notes/{id}/transcript     -- form/json transcript edit
    PATCH /voice-notes/{id}/attachments    -- HTMX add/remove single attachment
    POST  /voice-notes/{id}/re-extract     -- re-run extraction on edited text
    DELETE /voice-notes/{id}               -- delete (UI alias of API delete)

The existing JSON API is kept intact:
    POST   /api/voice-notes/preview-attachments
    POST   /api/voice-notes
    GET    /api/voice-notes
    GET    /api/voice-notes/{id}
    PATCH  /api/voice-notes/{id}
    DELETE /api/voice-notes/{id}
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import (
    ActionItem,
    ChatAsk,
    EmailAsk,
    Person,
    VoiceNote,
    VoiceNoteAttachment,
    Workstream,
)
from aegis.db.repositories import MeetingsRepository
from aegis.db.voice_notes_repository import VoiceNotesRepository
from aegis.processing import resolver as resolver_mod
from aegis.web import templates
from aegis.web.breadcrumb import resolve_breadcrumb

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()

# How far back/forward of ``started_at`` we look for an in-progress meeting
# when ``is_excerpt=true``. Window must be wide enough to catch a long
# meeting that started a while ago and is still running.
_EXCERPT_LOOKBACK = timedelta(hours=4)
_EXCERPT_LOOKAHEAD = timedelta(minutes=5)

_LIST_PAGE_SIZE = 25


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.aegis_timezone)


# ── Wire-format schemas (existing API) ──────────────────────


class PreviewAttachmentsRequest(BaseModel):
    transcript_text: str
    occurred_at: float
    confidence_threshold: float = 0.6


class AttachmentSuggestion(BaseModel):
    target_type: Literal["person", "workstream", "email_ask", "chat_ask"]
    target_id: int
    label: str
    confidence: float


class EntityMatch(BaseModel):
    target_type: Literal["person", "workstream", "email_ask", "chat_ask"]
    target_id: int
    label: str
    confidence: float
    span: str


class PreviewAttachmentsResponse(BaseModel):
    suggested_attachments: list[AttachmentSuggestion]
    matches: list[EntityMatch]


class VoiceNoteAttachmentInput(BaseModel):
    target_type: Literal["person", "workstream", "email_ask", "chat_ask"]
    target_id: int
    is_suggested: bool = False
    confirmed_at: datetime | None = None


class VoiceNoteCreateRequest(BaseModel):
    helios_voice_note_id: int
    helios_session_id: int
    started_at: float
    ended_at: float
    duration_seconds: float
    transcript_text: str
    triggered_by: Literal["menu_bar", "hotkey", "dashboard"]
    is_excerpt: bool = False
    attachments: list[VoiceNoteAttachmentInput] = Field(default_factory=list)
    source_device: str = "mac"


class VoiceNoteCreateResponse(BaseModel):
    voice_note_id: int
    extraction_status: Literal["queued"]


class VoiceNoteAttachmentResponse(BaseModel):
    target_type: Literal["person", "workstream", "email_ask", "chat_ask"]
    target_id: int
    is_suggested: bool
    confirmed_at: datetime | None


class VoiceNoteResponse(BaseModel):
    id: int
    helios_voice_note_id: int
    helios_session_id: int
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    transcript_text: str
    transcript_text_edited: str | None
    triggered_by: Literal["menu_bar", "hotkey", "dashboard"]
    is_excerpt: bool
    excerpt_of_meeting_id: int | None
    source_device: str
    processing_status: Literal["pending", "processing", "completed", "failed"]
    attachments: list[VoiceNoteAttachmentResponse]


class VoiceNotePatchRequest(BaseModel):
    transcript_text_edited: str | None = None
    attachments: list[VoiceNoteAttachmentInput] | None = None


# ── Helpers ────────────────────────────────────────────────


def _voice_note_to_response(vn) -> VoiceNoteResponse:
    attachments = [
        VoiceNoteAttachmentResponse(
            target_type=a.target_type,
            target_id=a.target_id,
            is_suggested=a.is_suggested,
            confirmed_at=a.confirmed_at,
        )
        for a in (vn.attachments or [])
    ]
    return VoiceNoteResponse(
        id=vn.id,
        helios_voice_note_id=vn.helios_voice_note_id,
        helios_session_id=vn.helios_session_id,
        started_at=vn.started_at,
        ended_at=vn.ended_at,
        duration_seconds=vn.duration_seconds,
        transcript_text=vn.transcript_text,
        transcript_text_edited=vn.transcript_text_edited,
        triggered_by=vn.triggered_by,
        is_excerpt=vn.is_excerpt,
        excerpt_of_meeting_id=vn.excerpt_of_meeting_id,
        source_device=vn.source_device,
        processing_status=vn.processing_status,
        attachments=attachments,
    )


async def _find_excerpt_meeting_id(
    session: AsyncSession, started_at: datetime
) -> int | None:
    repo = MeetingsRepository(session)
    candidates = await repo.get_in_range(
        started_at - _EXCERPT_LOOKBACK,
        started_at + _EXCERPT_LOOKAHEAD,
    )
    overlapping = [
        m for m in candidates
        if m.start_time <= started_at <= m.end_time
    ]
    if not overlapping:
        return None
    overlapping.sort(key=lambda m: started_at - m.start_time)
    return overlapping[0].id


async def _resolve_attachment_labels(
    session: AsyncSession, attachments
) -> dict[tuple[str, int], str]:
    """Look up display labels for a set of (target_type, target_id) pairs.

    Critical #4: ``email_ask`` and ``chat_ask`` are queried against their
    respective tables, not the (no-longer-existent) combined ``ask``
    type. Numeric ids can collide across email_asks and chat_asks; the
    target_type distinguishes them.
    """
    person_ids: list[int] = []
    workstream_ids: list[int] = []
    email_ask_ids: list[int] = []
    chat_ask_ids: list[int] = []
    for a in attachments or []:
        if a.target_type == "person":
            person_ids.append(a.target_id)
        elif a.target_type == "workstream":
            workstream_ids.append(a.target_id)
        elif a.target_type == "email_ask":
            email_ask_ids.append(a.target_id)
        elif a.target_type == "chat_ask":
            chat_ask_ids.append(a.target_id)

    labels: dict[tuple[str, int], str] = {}

    if person_ids:
        rows = (
            await session.execute(
                select(Person.id, Person.name).where(Person.id.in_(person_ids))
            )
        ).all()
        for row in rows:
            labels[("person", row[0])] = row[1] or f"Person #{row[0]}"
    if workstream_ids:
        rows = (
            await session.execute(
                select(Workstream.id, Workstream.name).where(
                    Workstream.id.in_(workstream_ids)
                )
            )
        ).all()
        for row in rows:
            labels[("workstream", row[0])] = row[1] or f"Workstream #{row[0]}"
    if email_ask_ids:
        rows = (
            await session.execute(
                select(EmailAsk.id, EmailAsk.description).where(
                    EmailAsk.id.in_(email_ask_ids)
                )
            )
        ).all()
        for row in rows:
            text = row[1] or ""
            labels[("email_ask", row[0])] = (
                (text[:60] + "…") if len(text) > 60
                else (text or f"Email ask #{row[0]}")
            )
    if chat_ask_ids:
        rows = (
            await session.execute(
                select(ChatAsk.id, ChatAsk.description).where(
                    ChatAsk.id.in_(chat_ask_ids)
                )
            )
        ).all()
        for row in rows:
            text = row[1] or ""
            labels[("chat_ask", row[0])] = (
                (text[:60] + "…") if len(text) > 60
                else (text or f"Chat ask #{row[0]}")
            )
    return labels


# ═══════════════════════════════════════════════════════════
# JSON API endpoints
# ═══════════════════════════════════════════════════════════


@router.post(
    "/api/voice-notes/preview-attachments",
    response_model=PreviewAttachmentsResponse,
)
async def preview_attachments(
    req: PreviewAttachmentsRequest,
    session: AsyncSession = Depends(get_session),
) -> PreviewAttachmentsResponse:
    matches = await resolver_mod.resolve_text(session, req.transcript_text)
    matches_payload: list[EntityMatch] = []
    for m in matches:
        if m.target_type not in (
            "person", "workstream", "email_ask", "chat_ask"
        ):
            continue
        matches_payload.append(
            EntityMatch(
                target_type=m.target_type,
                target_id=m.target_id,
                label=m.label,
                confidence=m.confidence,
                span=m.span,
            )
        )
    suggested = [
        AttachmentSuggestion(
            target_type=m.target_type,
            target_id=m.target_id,
            label=m.label,
            confidence=m.confidence,
        )
        for m in matches_payload
        if m.confidence >= req.confidence_threshold
    ]
    return PreviewAttachmentsResponse(
        suggested_attachments=suggested,
        matches=matches_payload,
    )


@router.post("/api/voice-notes", response_model=VoiceNoteCreateResponse)
async def create_voice_note(
    req: VoiceNoteCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> VoiceNoteCreateResponse:
    repo = VoiceNotesRepository(session)

    existing = await repo.get_by_helios_id(req.helios_voice_note_id)
    if existing is not None:
        return VoiceNoteCreateResponse(
            voice_note_id=existing.id,
            extraction_status="queued",
        )

    started_dt = datetime.fromtimestamp(req.started_at, tz=timezone.utc)
    ended_dt = datetime.fromtimestamp(req.ended_at, tz=timezone.utc)

    excerpt_of_meeting_id: int | None = None
    if req.is_excerpt:
        excerpt_of_meeting_id = await _find_excerpt_meeting_id(session, started_dt)

    vn = await repo.create(
        helios_voice_note_id=req.helios_voice_note_id,
        helios_session_id=req.helios_session_id,
        started_at=started_dt,
        ended_at=ended_dt,
        duration_seconds=req.duration_seconds,
        transcript_text=req.transcript_text,
        triggered_by=req.triggered_by,
        is_excerpt=req.is_excerpt,
        excerpt_of_meeting_id=excerpt_of_meeting_id,
        source_device=req.source_device,
    )

    if req.attachments:
        await repo.update_attachments(
            vn.id,
            [a.model_dump() for a in req.attachments],
        )

    await session.commit()
    return VoiceNoteCreateResponse(
        voice_note_id=vn.id,
        extraction_status="queued",
    )


@router.get("/api/voice-notes", response_model=list[VoiceNoteResponse])
async def list_voice_notes(
    triggered_by: Literal["menu_bar", "hotkey", "dashboard"] | None = Query(default=None),
    is_excerpt: bool | None = Query(default=None),
    processing_status: Literal[
        "pending", "processing", "completed", "failed"
    ] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[VoiceNoteResponse]:
    repo = VoiceNotesRepository(session)
    notes = await repo.list(
        limit=limit,
        offset=offset,
        triggered_by=triggered_by,
        is_excerpt=is_excerpt,
        processing_status=processing_status,
    )
    return [_voice_note_to_response(n) for n in notes]


@router.get("/api/voice-notes/{voice_note_id}", response_model=VoiceNoteResponse)
async def get_voice_note(
    voice_note_id: int,
    session: AsyncSession = Depends(get_session),
) -> VoiceNoteResponse:
    repo = VoiceNotesRepository(session)
    vn = await repo.get_by_id(voice_note_id)
    if vn is None:
        raise HTTPException(status_code=404, detail="voice note not found")
    return _voice_note_to_response(vn)


@router.patch("/api/voice-notes/{voice_note_id}", response_model=VoiceNoteResponse)
async def patch_voice_note(
    voice_note_id: int,
    req: VoiceNotePatchRequest,
    session: AsyncSession = Depends(get_session),
) -> VoiceNoteResponse:
    repo = VoiceNotesRepository(session)

    if req.transcript_text_edited is None and req.attachments is None:
        vn = await repo.get_by_id(voice_note_id)
        if vn is None:
            raise HTTPException(status_code=404, detail="voice note not found")
        return _voice_note_to_response(vn)

    if req.transcript_text_edited is not None:
        result = await repo.update_transcript_edit(
            voice_note_id, req.transcript_text_edited
        )
        if result is None:
            raise HTTPException(status_code=404, detail="voice note not found")

    if req.attachments is not None:
        result = await repo.update_attachments(
            voice_note_id,
            [a.model_dump() for a in req.attachments],
        )
        if result is None:
            raise HTTPException(status_code=404, detail="voice note not found")

    await session.commit()

    vn = await repo.get_by_id(voice_note_id)
    if vn is None:
        raise HTTPException(status_code=404, detail="voice note not found")
    return _voice_note_to_response(vn)


@router.delete(
    "/api/voice-notes/{voice_note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_voice_note_api(
    voice_note_id: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    repo = VoiceNotesRepository(session)
    deleted = await repo.delete(voice_note_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="voice note not found")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ═══════════════════════════════════════════════════════════
# UI page routes
# ═══════════════════════════════════════════════════════════


@router.get("/voice-notes", response_class=HTMLResponse)
async def voice_notes_list_page(
    request: Request,
    triggered_by: str = Query(default=""),
    is_excerpt: str = Query(default=""),
    has_attachments: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    session: AsyncSession = Depends(get_session),
):
    """Voice notes list page (reverse chronological, filterable)."""
    tz = _local_tz()

    stmt = select(VoiceNote).options(selectinload(VoiceNote.attachments))
    count_stmt = select(func.count()).select_from(VoiceNote)

    if triggered_by in ("menu_bar", "hotkey", "dashboard"):
        stmt = stmt.where(VoiceNote.triggered_by == triggered_by)
        count_stmt = count_stmt.where(VoiceNote.triggered_by == triggered_by)

    if is_excerpt == "true":
        stmt = stmt.where(VoiceNote.is_excerpt.is_(True))
        count_stmt = count_stmt.where(VoiceNote.is_excerpt.is_(True))
    elif is_excerpt == "false":
        stmt = stmt.where(VoiceNote.is_excerpt.is_(False))
        count_stmt = count_stmt.where(VoiceNote.is_excerpt.is_(False))

    if has_attachments in ("true", "false"):
        # Subquery-based filter so we don't mess with the main ordering.
        attached_subq = select(VoiceNoteAttachment.voice_note_id).distinct()
        if has_attachments == "true":
            stmt = stmt.where(VoiceNote.id.in_(attached_subq))
            count_stmt = count_stmt.where(VoiceNote.id.in_(attached_subq))
        else:
            stmt = stmt.where(~VoiceNote.id.in_(attached_subq))
            count_stmt = count_stmt.where(~VoiceNote.id.in_(attached_subq))

    if date_from:
        try:
            start_local = datetime.fromisoformat(date_from).replace(tzinfo=tz)
            stmt = stmt.where(VoiceNote.started_at >= start_local.astimezone(timezone.utc))
            count_stmt = count_stmt.where(
                VoiceNote.started_at >= start_local.astimezone(timezone.utc)
            )
        except ValueError:
            pass
    if date_to:
        try:
            end_local = datetime.fromisoformat(date_to).replace(tzinfo=tz) + timedelta(days=1)
            stmt = stmt.where(VoiceNote.started_at < end_local.astimezone(timezone.utc))
            count_stmt = count_stmt.where(
                VoiceNote.started_at < end_local.astimezone(timezone.utc)
            )
        except ValueError:
            pass

    total = (await session.execute(count_stmt)).scalar_one() or 0
    total_pages = max(1, math.ceil(total / _LIST_PAGE_SIZE))
    if page > total_pages:
        page = total_pages

    stmt = (
        stmt.order_by(VoiceNote.started_at.desc())
        .offset((page - 1) * _LIST_PAGE_SIZE)
        .limit(_LIST_PAGE_SIZE)
    )

    result = await session.execute(stmt)
    voice_notes = list(result.scalars().unique().all())

    base_params: list[str] = []
    for k, v in (
        ("triggered_by", triggered_by),
        ("is_excerpt", is_excerpt),
        ("has_attachments", has_attachments),
        ("date_from", date_from),
        ("date_to", date_to),
    ):
        if v:
            base_params.append(f"{k}={v}")
    querystring_prev = "&".join(base_params + [f"page={page - 1}"])
    querystring_next = "&".join(base_params + [f"page={page + 1}"])

    now_local = datetime.now(tz)
    return templates.TemplateResponse(
        request,
        "voice_notes/list.html",
        {
            "voice_notes": voice_notes,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "tz": tz,
            "filters": {
                "triggered_by": triggered_by,
                "is_excerpt": is_excerpt,
                "has_attachments": has_attachments,
                "date_from": date_from,
                "date_to": date_to,
            },
            "querystring_prev": querystring_prev,
            "querystring_next": querystring_next,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
        },
    )


@router.get("/voice-notes/{voice_note_id}", response_class=HTMLResponse)
async def voice_note_detail_page(
    request: Request,
    voice_note_id: int,
    from_url: str | None = Query(None, alias="from"),
    session: AsyncSession = Depends(get_session),
):
    """Voice note detail page (transcript edit + attachments)."""
    repo = VoiceNotesRepository(session)
    vn = await repo.get_by_id(voice_note_id)
    if vn is None:
        return HTMLResponse(
            '<div class="p-8 text-center text-red-600">Voice note not found</div>',
            status_code=404,
        )

    back_url, back_label = resolve_breadcrumb(
        request, from_url, "/voice-notes", "Voice Notes"
    )

    # Linked action items via source_voice_note_id.
    ai_stmt = (
        select(ActionItem)
        .where(ActionItem.source_voice_note_id == voice_note_id)
        .order_by(ActionItem.created.desc())
    )
    linked_action_items = list((await session.execute(ai_stmt)).scalars().all())

    attachment_labels = await _resolve_attachment_labels(session, vn.attachments)

    # Fetch the Helios session's mic-channel audio chunks so the detail
    # page can render an inline player. System-channel chunks are
    # dropped — voice notes are user-spoken, the mic is the source.
    audio_chunks: list[dict] = []
    if vn.helios_session_id:
        helios_client = request.app.state.helios_client
        payload = await helios_client.list_audio(
            session_id=vn.helios_session_id
        )
        for c in (payload or {}).get("chunks", []) or []:
            if c.get("channel") != "mic":
                continue
            if not c.get("has_audio_file"):
                continue
            audio_chunks.append(c)

    tz = _local_tz()
    now_local = datetime.now(tz)

    return templates.TemplateResponse(
        request,
        "voice_notes/detail.html",
        {
            "vn": vn,
            "linked_action_items": linked_action_items,
            "attachment_labels": attachment_labels,
            "audio_chunks": audio_chunks,
            "back_url": back_url,
            "back_label": back_label,
            "tz": tz,
            "current_time": now_local.strftime("%-I:%M %p %Z"),
        },
    )


@router.get("/voice-notes/{voice_note_id}/status", response_class=HTMLResponse)
async def voice_note_status_fragment(
    voice_note_id: int,
    session: AsyncSession = Depends(get_session),
):
    """HTMX poll fragment — returns the processing status pill.

    Used by the detail page when ``processing_status == 'processing'`` to
    refresh the pill every 2 s until it lands on ``completed`` or
    ``failed``. The polling trigger stops itself when the status moves
    out of ``processing``.
    """
    repo = VoiceNotesRepository(session)
    vn = await repo.get_by_id(voice_note_id)
    if vn is None:
        return HTMLResponse(
            '<span id="processing-status" class="inline-flex items-center rounded-full bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700">missing</span>'
        )

    trigger = 'every 2s' if vn.processing_status == "processing" else 'none'
    label = "Re-extracting…" if vn.processing_status == "processing" else vn.processing_status

    if vn.processing_status == "completed":
        cls = "bg-emerald-50 text-emerald-700"
    elif vn.processing_status == "processing":
        cls = "bg-aegis-50 text-aegis-700 animate-pulse"
    elif vn.processing_status == "failed":
        cls = "bg-red-50 text-red-700"
    else:
        cls = "bg-gray-100 text-gray-500"

    return HTMLResponse(
        f'<span id="processing-status" '
        f'hx-get="/voice-notes/{voice_note_id}/status" '
        f'hx-trigger="{trigger}" hx-swap="outerHTML" '
        f'class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium {cls}">'
        f"{label}</span>"
    )


# ── Transcript edit (UI) ───────────────────────────────────


@router.patch("/voice-notes/{voice_note_id}/transcript", response_class=HTMLResponse)
async def update_transcript(
    voice_note_id: int,
    transcript_text_edited: str | None = Form(default=None),
    body: dict | None = Body(default=None),
    session: AsyncSession = Depends(get_session),
):
    """Update ``transcript_text_edited`` and queue a re-extraction.

    Accepts either form data (HTMX) or JSON body. The original transcript
    is preserved in ``transcript_text``. After update we mark the note as
    ``processing`` and kick off ``process_voice_note`` in the background
    so the UI poll fragment can show progress.
    """
    new_text = transcript_text_edited
    if new_text is None and body is not None:
        new_text = body.get("transcript_text_edited")
    if new_text is None:
        raise HTTPException(status_code=400, detail="transcript_text_edited required")

    repo = VoiceNotesRepository(session)
    result = await repo.update_transcript_edit(voice_note_id, new_text)
    if result is None:
        raise HTTPException(status_code=404, detail="voice note not found")
    # Reset status so the re-extract is visible in the UI poll.
    await repo.mark_processing_status(voice_note_id, "processing")
    await session.commit()

    # Fire re-extraction in the background. We don't await — the UI
    # polls /voice-notes/{id}/status to detect completion.
    _schedule_reextract(voice_note_id)

    return HTMLResponse(
        '<span id="processing-status" '
        f'hx-get="/voice-notes/{voice_note_id}/status" '
        'hx-trigger="every 2s" hx-swap="outerHTML" '
        'class="inline-flex items-center rounded-full bg-aegis-50 px-2 py-0.5 text-xs font-medium text-aegis-700 animate-pulse">'
        "Re-extracting…</span>"
    )


# ── Attachment patch (UI) ──────────────────────────────────


@router.patch("/voice-notes/{voice_note_id}/attachments", response_class=HTMLResponse)
async def patch_attachments(
    voice_note_id: int,
    add_target_type: str | None = Form(default=None),
    add_target_id: int | None = Form(default=None),
    remove_target_type: str | None = Form(default=None),
    remove_target_id: int | None = Form(default=None),
    body: dict | None = Body(default=None),
    session: AsyncSession = Depends(get_session),
):
    """Add or remove a single attachment.

    Two modes:
      * Form fields ``add_target_type`` + ``add_target_id`` -- add an
        attachment (idempotent — duplicates dropped).
      * Form fields ``remove_target_type`` + ``remove_target_id`` --
        remove an attachment.

    Returns an empty body on remove (HTMX swaps the row out). Returns 204
    on missing notes.
    """
    if body is not None:
        add_target_type = add_target_type or body.get("add_target_type")
        add_target_id = add_target_id if add_target_id is not None else body.get("add_target_id")
        remove_target_type = remove_target_type or body.get("remove_target_type")
        remove_target_id = remove_target_id if remove_target_id is not None else body.get("remove_target_id")

    repo = VoiceNotesRepository(session)
    vn = await repo.get_by_id(voice_note_id)
    if vn is None:
        raise HTTPException(status_code=404, detail="voice note not found")

    existing = [
        {
            "target_type": a.target_type,
            "target_id": a.target_id,
            "is_suggested": a.is_suggested,
            "confirmed_at": a.confirmed_at,
        }
        for a in (vn.attachments or [])
    ]

    if remove_target_type and remove_target_id is not None:
        existing = [
            a
            for a in existing
            if not (
                a["target_type"] == remove_target_type
                and a["target_id"] == int(remove_target_id)
            )
        ]
    if add_target_type and add_target_id is not None:
        if add_target_type not in (
            "person", "workstream", "email_ask", "chat_ask"
        ):
            raise HTTPException(status_code=400, detail="invalid target_type")
        # Warning #7: dedup at the application layer so rapid duplicate
        # HTMX clicks don't trigger the UNIQUE constraint mid-flight.
        # ``update_attachments`` is delete-then-insert and the loop below
        # already short-circuits when the same key exists; the explicit
        # IntegrityError catch on save belt-and-braces this.
        key = (add_target_type, int(add_target_id))
        if not any((a["target_type"], a["target_id"]) == key for a in existing):
            existing.append(
                {
                    "target_type": add_target_type,
                    "target_id": int(add_target_id),
                    "is_suggested": False,
                    "confirmed_at": datetime.now(timezone.utc),
                }
            )

    try:
        await repo.update_attachments(voice_note_id, existing)
        await session.commit()
    except Exception as exc:
        # IntegrityError is the only realistic case here — a concurrent
        # duplicate-add race. Treat as a no-op: the desired final state
        # is already present.
        await session.rollback()
        if "unique" not in str(exc).lower() and "duplicate" not in str(exc).lower():
            raise
        logger.info(
            "patch_attachments duplicate detected (idempotent): vn=%s",
            voice_note_id,
        )

    # Empty body — HTMX will remove the row.
    return HTMLResponse("")


# ── Re-extract (UI) ─────────────────────────────────────────


@router.post("/voice-notes/{voice_note_id}/re-extract", response_class=HTMLResponse)
async def re_extract(
    voice_note_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Manually re-run extraction on the (possibly edited) transcript.

    The extractor at ``aegis.processing.voice_note_extractor`` is
    idempotent: temp=0, action items deduped by source_voice_note_id,
    attachments merged so user-confirmed targets survive. Calling this
    twice in a row produces the same outcome (modulo Haiku run-to-run
    drift; conservative because temp=0).
    """
    repo = VoiceNotesRepository(session)
    vn = await repo.get_by_id(voice_note_id)
    if vn is None:
        raise HTTPException(status_code=404, detail="voice note not found")

    # Replace existing action items derived from this voice note so we
    # don't grow duplicates on each re-extract. The voice-note
    # extractor's idempotency contract relies on this cleanup.
    from sqlalchemy import delete as sa_delete

    await session.execute(
        sa_delete(ActionItem).where(ActionItem.source_voice_note_id == voice_note_id)
    )
    await repo.mark_processing_status(voice_note_id, "processing")
    await session.commit()

    _schedule_reextract(voice_note_id)

    return HTMLResponse(
        '<span id="processing-status" '
        f'hx-get="/voice-notes/{voice_note_id}/status" '
        'hx-trigger="every 2s" hx-swap="outerHTML" '
        'class="inline-flex items-center rounded-full bg-aegis-50 px-2 py-0.5 text-xs font-medium text-aegis-700 animate-pulse">'
        "Re-extracting…</span>"
    )


def _schedule_reextract(voice_note_id: int) -> None:
    """Fire-and-forget background re-extraction.

    Lazy-imports the extractor so test collectors that monkeypatch the
    module don't have to bring in Anthropic at import time. Swallows
    extractor exceptions — the pipeline already marks the note as
    ``failed`` and logs.
    """
    async def _runner() -> None:
        try:
            from aegis.processing.voice_note_extractor import process_voice_note
            await process_voice_note(voice_note_id)
        except Exception:
            logger.exception(
                "background re-extraction failed for voice_note_id=%s",
                voice_note_id,
            )

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_runner())
    except RuntimeError:
        # No running loop (rare; safety net). Schedule with asyncio.run
        # in a worker thread so the request handler never blocks.
        import threading

        def _thread_target() -> None:
            try:
                asyncio.run(_runner())
            except Exception:
                logger.exception(
                    "thread re-extraction failed for voice_note_id=%s",
                    voice_note_id,
                )

        threading.Thread(target=_thread_target, daemon=True).start()


@router.delete(
    "/voice-notes/{voice_note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_voice_note_ui(
    voice_note_id: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """UI alias of ``DELETE /api/voice-notes/{id}`` for HTMX buttons."""
    repo = VoiceNotesRepository(session)
    deleted = await repo.delete(voice_note_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="voice note not found")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
