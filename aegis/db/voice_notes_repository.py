"""Voice notes repository — CRUD over voice_notes + voice_note_attachments.

Used by:
- POST /api/voice-notes (Wave 3K) — `create` + `update_attachments`
- GET /api/voice-notes (Wave 3K) — `list`, `get_by_id`, `get_by_helios_id`
- PATCH /api/voice-notes/{id} (Wave 3K) — `update_transcript_edit`,
  `update_attachments`
- DELETE /api/voice-notes/{id} (Wave 3K) — `delete`
- The extraction pipeline (Wave 4L) — `mark_processing_status`, `set_embedding`,
  and `list(processing_status="pending", ...)` to find work
- The dashboard (Phase 4) — `list_in_range`, `list_for_person`,
  `list_for_workstream`, `list_for_ask`

No raw SQL outside this file (project convention) — uses SQLAlchemy ORM
constructs (`select`, `delete`) only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from aegis.db.models import VoiceNote, VoiceNoteAttachment

TargetType = Literal["person", "workstream", "email_ask", "chat_ask"]
TriggeredBy = Literal["menu_bar", "hotkey", "dashboard"]
ProcessingStatus = Literal["pending", "processing", "completed", "failed"]


class VoiceNotesRepository:
    """CRUD over voice_notes + voice_note_attachments.

    Constructor takes an `AsyncSession`; methods are all async. The caller
    controls the `session.commit()` boundary — this repository only `flush`es
    so multiple operations can be batched into a single transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── create ────────────────────────────────────────────────

    async def create(
        self,
        *,
        helios_voice_note_id: int,
        helios_session_id: int,
        started_at: datetime,
        ended_at: datetime,
        duration_seconds: float,
        transcript_text: str,
        triggered_by: TriggeredBy,
        is_excerpt: bool = False,
        excerpt_of_meeting_id: int | None = None,
        source_device: str = "mac",
    ) -> VoiceNote:
        """Insert a new ``VoiceNote`` row. Caller manages ``session.commit()``.

        Raises ``IntegrityError`` if ``helios_voice_note_id`` already exists
        (it has a UNIQUE constraint).
        """
        vn = VoiceNote(
            helios_voice_note_id=helios_voice_note_id,
            helios_session_id=helios_session_id,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=duration_seconds,
            transcript_text=transcript_text,
            triggered_by=triggered_by,
            is_excerpt=is_excerpt,
            excerpt_of_meeting_id=excerpt_of_meeting_id,
            source_device=source_device,
            processing_status="pending",
        )
        self._session.add(vn)
        await self._session.flush()
        return vn

    # ── read ──────────────────────────────────────────────────

    async def get_by_id(self, voice_note_id: int) -> VoiceNote | None:
        """Fetch a voice note by primary key with attachments eager-loaded."""
        stmt = (
            select(VoiceNote)
            .where(VoiceNote.id == voice_note_id)
            .options(selectinload(VoiceNote.attachments))
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_helios_id(
        self, helios_voice_note_id: int
    ) -> VoiceNote | None:
        """Fetch a voice note by its Helios-side ID (unique).

        Used by the create endpoint for idempotency: if Helios retries a POST
        with the same ``helios_voice_note_id``, the endpoint can detect the
        conflict and return the existing row.
        """
        stmt = (
            select(VoiceNote)
            .where(VoiceNote.helios_voice_note_id == helios_voice_note_id)
            .options(selectinload(VoiceNote.attachments))
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        triggered_by: str | None = None,
        is_excerpt: bool | None = None,
        processing_status: str | None = None,
    ) -> list[VoiceNote]:
        """List voice notes ordered by ``started_at`` desc.

        ``processing_status="pending"`` is the extraction pipeline's filter
        for finding work. Attachments are eager-loaded.
        """
        stmt = select(VoiceNote).options(selectinload(VoiceNote.attachments))
        if triggered_by is not None:
            stmt = stmt.where(VoiceNote.triggered_by == triggered_by)
        if is_excerpt is not None:
            stmt = stmt.where(VoiceNote.is_excerpt == is_excerpt)
        if processing_status is not None:
            stmt = stmt.where(VoiceNote.processing_status == processing_status)
        stmt = (
            stmt.order_by(VoiceNote.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_in_range(
        self, start_dt: datetime, end_dt: datetime
    ) -> list[VoiceNote]:
        """Fetch voice notes whose ``started_at`` falls in [start, end]."""
        stmt = (
            select(VoiceNote)
            .where(
                and_(
                    VoiceNote.started_at >= start_dt,
                    VoiceNote.started_at <= end_dt,
                )
            )
            .order_by(VoiceNote.started_at)
            .options(selectinload(VoiceNote.attachments))
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_target(
        self,
        target_type: TargetType,
        target_id: int,
        *,
        limit: int = 50,
    ) -> list[VoiceNote]:
        """All voice notes that have an attachment to the given target.

        Used by the dashboard person/workstream/ask detail pages. Joins
        through ``voice_note_attachments`` and de-duplicates (a single
        voice note may have multiple attachments to the same target — though
        the unique constraint actually prevents that, the ``.unique()`` on
        the result is defensive).
        """
        stmt = (
            select(VoiceNote)
            .join(
                VoiceNoteAttachment,
                VoiceNoteAttachment.voice_note_id == VoiceNote.id,
            )
            .where(
                and_(
                    VoiceNoteAttachment.target_type == target_type,
                    VoiceNoteAttachment.target_id == target_id,
                )
            )
            .order_by(VoiceNote.started_at.desc())
            .limit(limit)
            .options(selectinload(VoiceNote.attachments))
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().unique().all())

    # Convenience aliases — spec lists separate methods per target type.
    async def list_for_person(
        self, person_id: int, *, limit: int = 50
    ) -> list[VoiceNote]:
        return await self.list_for_target("person", person_id, limit=limit)

    async def list_for_workstream(
        self, workstream_id: int, *, limit: int = 50
    ) -> list[VoiceNote]:
        return await self.list_for_target(
            "workstream", workstream_id, limit=limit
        )

    async def list_for_email_ask(
        self, email_ask_id: int, *, limit: int = 50
    ) -> list[VoiceNote]:
        return await self.list_for_target(
            "email_ask", email_ask_id, limit=limit
        )

    async def list_for_chat_ask(
        self, chat_ask_id: int, *, limit: int = 50
    ) -> list[VoiceNote]:
        return await self.list_for_target(
            "chat_ask", chat_ask_id, limit=limit
        )

    async def list_for_ask(
        self,
        ask_id: int,
        *,
        source: Literal["email", "chat"],
        limit: int = 50,
    ) -> list[VoiceNote]:
        """Lookup voice notes by ask id + source. Critical #4 disambiguates
        the previous ``target_type='ask'`` collision — callers MUST pass
        ``source`` to indicate which ask table the id belongs to.
        """
        target_type: TargetType = (
            "email_ask" if source == "email" else "chat_ask"
        )
        return await self.list_for_target(target_type, ask_id, limit=limit)

    # ── update ────────────────────────────────────────────────

    async def update_transcript_edit(
        self, voice_note_id: int, transcript_text_edited: str
    ) -> VoiceNote | None:
        """Set ``transcript_text_edited`` (the user's edited version).

        The original ``transcript_text`` is never overwritten — the dashboard
        always shows the edited version when present, falling back to the
        original. Returns ``None`` if the note doesn't exist.
        """
        vn = await self.get_by_id(voice_note_id)
        if vn is None:
            return None
        vn.transcript_text_edited = transcript_text_edited
        await self._session.flush()
        return vn

    async def update_attachments(
        self,
        voice_note_id: int,
        attachments: list[dict],
    ) -> VoiceNote | None:
        """Replace the attachment set for a voice note (atomic delete+insert).

        Each entry is a dict with keys: ``target_type`` (TargetType),
        ``target_id`` (int), ``is_suggested`` (bool, default False), and
        optionally ``confirmed_at`` (datetime — if absent, server default is
        ``now()``).

        We delete all existing rows for this voice_note then insert the new
        set. The unique constraint on (voice_note_id, target_type, target_id)
        makes naive upsert risky in mid-flight, so the delete-then-insert
        approach lets the same input survive repeated calls (idempotent at
        the application layer).

        Returns the updated voice note (with the new attachment set
        eager-loaded), or ``None`` if the note doesn't exist.
        """
        vn = await self.get_by_id(voice_note_id)
        if vn is None:
            return None
        await self._session.execute(
            delete(VoiceNoteAttachment).where(
                VoiceNoteAttachment.voice_note_id == voice_note_id
            )
        )
        for att in attachments:
            new_attachment = VoiceNoteAttachment(
                voice_note_id=voice_note_id,
                target_type=att["target_type"],
                target_id=att["target_id"],
                is_suggested=att.get("is_suggested", False),
            )
            # Only set confirmed_at if explicitly supplied; otherwise let the
            # column server_default ('now()') populate it.
            if "confirmed_at" in att and att["confirmed_at"] is not None:
                new_attachment.confirmed_at = att["confirmed_at"]
            self._session.add(new_attachment)
        await self._session.flush()
        # Re-fetch with attachments populated.
        return await self.get_by_id(voice_note_id)

    async def mark_processing_status(
        self,
        voice_note_id: int,
        status: ProcessingStatus,
    ) -> None:
        """Update ``processing_status`` on the voice note.

        Used by the extraction pipeline: ``pending`` → ``processing`` →
        ``completed``/``failed``. No-ops if the note doesn't exist.
        """
        vn = await self.get_by_id(voice_note_id)
        if vn is None:
            return
        vn.processing_status = status
        await self._session.flush()

    async def set_embedding(
        self, voice_note_id: int, embedding: list[float]
    ) -> None:
        """Persist a 1536-dim embedding on the voice note.

        Caller is responsible for generating the vector via
        ``aegis.processing.embeddings.embed_text`` (or equivalent).
        """
        vn = await self.get_by_id(voice_note_id)
        if vn is None:
            return
        vn.embedding = embedding
        await self._session.flush()

    # ── delete ────────────────────────────────────────────────

    async def delete(self, voice_note_id: int) -> bool:
        """Delete a voice note. Cascades to attachments via FK ON DELETE CASCADE.

        Returns True if a row was deleted, False if the note didn't exist.
        """
        result = await self._session.execute(
            delete(VoiceNote).where(VoiceNote.id == voice_note_id)
        )
        await self._session.flush()
        return (result.rowcount or 0) > 0
