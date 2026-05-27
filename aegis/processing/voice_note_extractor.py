"""Voice note extraction — triage, extract entities, resolve, embed.

Per HELIOS.md §16.12. Voice notes are short (<=5 min) user-spoken
captures triaged + extracted via Haiku 4.5 (same model as
``meeting_extractor``) with a prompt tuned for self-directed action
items.

Pipeline shape (mirrors meeting_extractor):

  1. extract_voice_note  — single Haiku call returning a structured
     ``VoiceNoteExtraction`` that includes the triage class. ``noise``
     short-circuits: caller skips entity persistence.
  2. store_voice_note_extraction
       a. Resolve people / workstreams / asks via
          ``aegis.processing.resolver.resolve_text``.
       b. MERGE resolver matches into the voice note's existing
          attachment set without overwriting user-confirmed rows
          (``is_suggested=False``). User-confirmed attachments survive
          re-extraction; resolver matches are added as ``is_suggested``.
       c. Create ``ActionItem`` rows for each extracted action item.
       d. Generate a 1536-dim embedding via ``embed_text``.
       e. Best-effort link the voice note to one or more workstreams
          using cosine similarity against active workstream embeddings.
  3. process_voice_note  — entry point used by ``pipeline.py``. Marks
     ``processing_status`` (``processing`` -> ``completed``/``failed``)
     and rolls back on any exception.

Logging policy: voice-note transcripts are PII. We log only IDs,
counts, and class labels — never transcript content.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import async_session_factory
from aegis.db.models import ActionItem, LLMUsage, VoiceNote, Workstream
from aegis.db.repositories import link_item_to_workstream
from aegis.db.voice_notes_repository import VoiceNotesRepository
from aegis.processing import resolver as resolver_mod
from aegis.processing.embeddings import embed_text

logger = logging.getLogger(__name__)

EXTRACTION_MODEL = "claude-haiku-4-5-20251001"

# ── Confidence thresholds ────────────────────────────────────
# Resolver matches above this score become "suggested" attachments.
_SUGGESTED_CONFIDENCE_FLOOR = 0.6
# Cosine similarity floor for auto-linking a voice note to an existing
# workstream is read from settings (``voice_note_workstream_floor`` —
# see ``aegis/config.py``). Conservative default 0.55. Voice notes are
# short and chatty so we err on the side of leaving them unlinked
# rather than mis-linking.


# ── Pydantic schemas for Haiku output ────────────────────────


class _ExtractedActionItem(BaseModel):
    description: str
    deadline: str | None = None
    related_person: str | None = None  # mentioned person to follow up with


class VoiceNoteExtraction(BaseModel):
    """Parsed Haiku output for a single voice note."""

    triage_class: str = Field(
        ..., description="substantive | contextual | noise"
    )
    summary: str = ""
    action_items: list[_ExtractedActionItem] = Field(default_factory=list)
    mentioned_people: list[str] = Field(default_factory=list)
    mentioned_workstreams: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


# ── Prompt ───────────────────────────────────────────────────


EXTRACTION_PROMPT = """\
You extract structured intelligence from a single user's voice note.
The user is talking to themselves — typical patterns include "note to
self", "remind me to", "follow up with X about Y". The speaker is
ALWAYS the user; do not try to attribute statements to anyone else.

Triage class — pick exactly one:
- substantive (0.7-1.0): contains an action item, decision, or specific
  information worth remembering.
- contextual  (0.3-0.7): adds context to existing work but no new ask.
- noise       (0.0-0.3): test recordings, accidental triggers, no
  useful content (e.g. "test test one two three").

For action_items: the user is the assignee unless they explicitly
delegate ("ask Sarah to review"). Include related_person ONLY when
the user mentions following up with a specific named person.

For mentioned_people / mentioned_workstreams: list ONLY entities the
user named. Free-text — entity resolution happens downstream.

For topics: 1-5 short keywords/phrases.

Return ONLY valid JSON matching this exact schema (no markdown, no
code blocks):
{{
  "triage_class": "substantive|contextual|noise",
  "summary": "string (1-2 sentences)",
  "action_items": [
    {{"description": "string", "deadline": "string or null", "related_person": "string or null"}}
  ],
  "mentioned_people": ["string"],
  "mentioned_workstreams": ["string"],
  "topics": ["string"]
}}

Voice note transcript:
{transcript}
"""


# ── Helpers ──────────────────────────────────────────────────


def _strip_code_fence(s: str) -> str:
    """Strip Markdown code fence wrapping if Haiku adds it."""
    s = s.strip()
    if "```" in s:
        # Pull the first fenced block out.
        s = s.split("```", 2)[1] if s.startswith("```") else s.split("```")[1]
        if s.startswith("json"):
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _transcript_for(voice_note: VoiceNote) -> str:
    """Prefer the user-edited transcript when present."""
    return (voice_note.transcript_text_edited or voice_note.transcript_text or "").strip()


async def _track_usage(
    session: AsyncSession,
    *,
    input_tokens: int,
    output_tokens: int,
    task: str = "voice_note_extraction",
) -> None:
    """Upsert daily LLM usage. Mirrors meeting_extractor._track_usage."""
    today = date.today()
    stmt = pg_insert(LLMUsage).values(
        date=today,
        model=EXTRACTION_MODEL,
        task=task,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        calls=1,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_llm_usage_daily",
        set_={
            "input_tokens": LLMUsage.input_tokens + input_tokens,
            "output_tokens": LLMUsage.output_tokens + output_tokens,
            "calls": LLMUsage.calls + 1,
        },
    )
    await session.execute(stmt)
    # Caller commits.


# ── Public surface ───────────────────────────────────────────


async def extract_voice_note(
    session: AsyncSession,
    voice_note: VoiceNote,
) -> VoiceNoteExtraction | None:
    """Triage + extract a voice note via Haiku.

    Returns the parsed ``VoiceNoteExtraction`` on success. Returns
    ``None`` when the voice note triages as ``noise`` so the caller can
    short-circuit entity persistence.

    The transcript itself is never logged. We log voice_note_id, the
    triage class label, and counts only.
    """
    transcript = _transcript_for(voice_note)
    if not transcript:
        # Empty transcript — treat as noise without burning a Haiku call.
        logger.info(
            "voice_note %s has empty transcript; triage=noise (skipped)",
            voice_note.id,
        )
        return None

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Voice notes are short; the 30k-char clamp is defensive.
    truncated = transcript[:30_000]
    prompt = EXTRACTION_PROMPT.format(transcript=truncated)

    response = await client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=2048,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text
    raw = _strip_code_fence(raw)

    try:
        parsed_dict = json.loads(raw)
        extraction = VoiceNoteExtraction(**parsed_dict)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.error(
            "voice_note %s extraction parse failed: %s",
            voice_note.id,
            type(exc).__name__,
        )
        raise

    await _track_usage(
        session,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

    logger.info(
        "extracted voice_note %s: triage=%s, action_items=%d, mentions=%d",
        voice_note.id,
        extraction.triage_class,
        len(extraction.action_items),
        len(extraction.mentioned_people) + len(extraction.mentioned_workstreams),
    )

    if extraction.triage_class == "noise":
        return None
    return extraction


async def store_voice_note_extraction(
    session: AsyncSession,
    voice_note: VoiceNote,
    extraction: VoiceNoteExtraction,
) -> None:
    """Persist extracted entities + suggested attachments + embedding.

    Critical: user-confirmed attachments (``is_suggested=False``) on the
    voice note MUST survive this call. ``update_attachments`` does an
    atomic delete-then-insert, so we build the final attachment set as
    the union of:

        keep   = user-confirmed rows already on the note
        addnew = resolver matches not already in ``keep``

    Resolver matches that target an entity the user already confirmed
    are dropped — re-suggesting something the user already accepted
    would flip ``is_suggested`` from False to True, losing the
    confirmation signal.
    """
    repo = VoiceNotesRepository(session)
    transcript = _transcript_for(voice_note)

    # ── 1. Resolve free text against People / Workstreams / Asks ──
    matches: list[resolver_mod.ResolverMatch] = []
    if transcript:
        try:
            matches = await resolver_mod.resolve_text(session, transcript)
        except Exception:
            logger.exception(
                "voice_note %s resolver failed; continuing without suggestions",
                voice_note.id,
            )
            matches = []

    # ── 2. Build merged attachment set ─────────────────────────
    user_confirmed_dicts: list[dict[str, Any]] = []
    for att in (voice_note.attachments or []):
        if not att.is_suggested:
            user_confirmed_dicts.append(
                {
                    "target_type": att.target_type,
                    "target_id": att.target_id,
                    "is_suggested": False,
                    "confirmed_at": att.confirmed_at,
                }
            )
    seen: set[tuple[str, int]] = {
        (a["target_type"], a["target_id"]) for a in user_confirmed_dicts
    }
    suggested_new: list[dict[str, Any]] = []
    for m in matches:
        if m.target_type not in (
            "person", "workstream", "email_ask", "chat_ask"
        ):
            continue
        if m.confidence < _SUGGESTED_CONFIDENCE_FLOOR:
            continue
        key = (m.target_type, m.target_id)
        if key in seen:
            continue
        suggested_new.append(
            {
                "target_type": m.target_type,
                "target_id": m.target_id,
                "is_suggested": True,
                "confirmed_at": None,
            }
        )
        seen.add(key)

    merged = user_confirmed_dicts + suggested_new
    # Only call update_attachments if the merged set differs from what's
    # already on the row. Voice notes that came in with no resolver
    # matches AND only user-confirmed rows would otherwise round-trip
    # through delete+insert for no reason.
    existing_pairs = {
        (att.target_type, att.target_id)
        for att in (voice_note.attachments or [])
    }
    new_pairs = {(a["target_type"], a["target_id"]) for a in merged}
    if existing_pairs != new_pairs or suggested_new:
        await repo.update_attachments(voice_note.id, merged)

    # ── 3. Action items ────────────────────────────────────────
    # Build a name -> person_id map from resolver matches for related_person.
    person_by_label: dict[str, int] = {}
    for m in matches:
        if m.target_type == "person":
            person_by_label[m.label.lower()] = m.target_id

    for item in extraction.action_items:
        related_person_id: int | None = None
        if item.related_person:
            needle = item.related_person.strip().lower()
            # Exact match first, then substring against the candidate label.
            if needle in person_by_label:
                related_person_id = person_by_label[needle]
            else:
                for label, pid in person_by_label.items():
                    if needle in label or label in needle:
                        related_person_id = pid
                        break

        # Best-effort embedding for the action item itself.
        action_embedding: list[float] | None = None
        try:
            action_embedding = await embed_text(item.description)
        except Exception:
            logger.exception(
                "voice_note %s: action-item embedding failed", voice_note.id
            )
            action_embedding = None

        # Self-directed action items convention (HELIOS.md §16.12):
        #   assignee_id IS NULL  AND  source_voice_note_id IS NOT NULL
        #     → action item the user spoke for themselves ("note to self").
        #   assignee_id IS NOT NULL  AND  source_voice_note_id IS NOT NULL
        #     → user explicitly delegated ("ask Sarah to ...").
        # Downstream readiness/dashboard queries that need the
        # "for-me" view should treat the first case as belonging to
        # the user and not surface it under "delegated to nobody".
        # No dedicated ``self_directed`` column — the source +
        # nullness combination is the signal.
        ai = ActionItem(
            description=item.description,
            assignee_id=related_person_id,
            source_voice_note_id=voice_note.id,
            deadline=item.deadline,
            status="open",
            embedding=action_embedding,
        )
        session.add(ai)
    await session.flush()

    # ── 4. Embedding on the voice note itself ──────────────────
    embedding: list[float] | None = None
    try:
        embedding = await embed_text(transcript or extraction.summary or "")
    except Exception:
        logger.exception("voice_note %s: embedding failed", voice_note.id)
    if embedding is not None:
        await repo.set_embedding(voice_note.id, embedding)

    # ── 5. Workstream assignment (best effort) ─────────────────
    if embedding is not None and any(v != 0.0 for v in embedding[:10]):
        try:
            await _assign_voice_note_to_workstreams(
                session, voice_note_id=voice_note.id, embedding=embedding
            )
        except Exception:
            logger.exception(
                "voice_note %s: workstream assignment failed",
                voice_note.id,
            )

    logger.info(
        "stored voice_note %s extraction: action_items=%d, attachments=%d",
        voice_note.id,
        len(extraction.action_items),
        len(merged),
    )


async def _assign_voice_note_to_workstreams(
    session: AsyncSession,
    *,
    voice_note_id: int,
    embedding: list[float],
) -> int:
    """Link the voice note to active workstreams whose embedding is similar.

    Mirrors ``workstream_detector.run_workstream_assignment`` for the
    single-item case. Inline here (rather than calling that batch
    routine) because the detector queries ``unassigned_items`` from the
    DB and isn't shaped for the per-item flow we want at extract time.
    """
    from aegis.processing.workstream_detector import cosine_similarity

    stmt = select(Workstream).where(
        Workstream.status == "active",
        Workstream.embedding.is_not(None),
    )
    result = await session.execute(stmt)
    workstreams = list(result.scalars().all())
    if not workstreams:
        return 0

    settings = get_settings()
    high = settings.workstream_assign_high_confidence
    floor = settings.voice_note_workstream_floor

    linked = 0
    for ws in workstreams:
        ws_emb = list(ws.embedding) if ws.embedding is not None else None
        if not ws_emb:
            continue
        sim = cosine_similarity(embedding, ws_emb)
        # Use the high-confidence threshold from settings, but never below
        # the conservative voice-note-specific floor.
        threshold = max(high, floor)
        if sim < threshold:
            continue
        await link_item_to_workstream(
            session,
            workstream_id=ws.id,
            item_type="voice_note",
            item_id=voice_note_id,
            linked_by="auto",
            relevance_score=sim,
        )
        linked += 1
    if linked:
        logger.info(
            "voice_note %s linked to %d workstream(s)", voice_note_id, linked
        )
    return linked


# ── Pipeline entry point ─────────────────────────────────────


async def process_voice_note(voice_note_id: int) -> bool:
    """Run the full extraction pipeline for one voice note.

    Returns ``True`` on success (including the noise-skip path),
    ``False`` if the note doesn't exist. Raises if extraction itself
    raises after ``processing_status`` is set to ``failed``.
    """
    # Phase A: mark as processing in its own short transaction.
    async with async_session_factory() as session:
        repo = VoiceNotesRepository(session)
        vn = await repo.get_by_id(voice_note_id)
        if vn is None:
            logger.info(
                "voice_note %s not found; skipping", voice_note_id
            )
            return False
        if vn.processing_status == "completed":
            logger.info(
                "voice_note %s already completed; skipping",
                voice_note_id,
            )
            return True
        await repo.mark_processing_status(voice_note_id, "processing")
        await session.commit()

    # Phase B: extract + store. Uses its own session; on any failure,
    # roll that session back, then mark failed in a fresh session.
    extraction: VoiceNoteExtraction | None = None
    try:
        async with async_session_factory() as session:
            repo = VoiceNotesRepository(session)
            vn = await repo.get_by_id(voice_note_id)
            if vn is None:
                # Deleted between phases — nothing to mark.
                return False
            extraction = await extract_voice_note(session, vn)
            if extraction is not None:
                await store_voice_note_extraction(session, vn, extraction)
            await repo.mark_processing_status(voice_note_id, "completed")
            await session.commit()
    except Exception:
        logger.exception(
            "voice_note %s extraction failed", voice_note_id
        )
        async with async_session_factory() as session:
            repo = VoiceNotesRepository(session)
            await repo.mark_processing_status(voice_note_id, "failed")
            await session.commit()
        raise

    return True


async def process_pending_voice_notes(limit: int = 50) -> int:
    """Pipeline scheduler hook: pick up pending voice notes and process them.

    Mirrors ``pipeline.process_pending_meetings``. Returns the count of
    voice notes successfully processed.
    """
    async with async_session_factory() as session:
        repo = VoiceNotesRepository(session)
        pending = await repo.list(processing_status="pending", limit=limit)
        ids = [vn.id for vn in pending]

    count = 0
    for vid in ids:
        try:
            ok = await process_voice_note(vid)
            if ok:
                count += 1
        except Exception:
            # process_voice_note already logged + marked failed.
            continue

    logger.info(
        "voice_note pipeline: processed %d/%d pending notes",
        count,
        len(ids),
    )
    return count
