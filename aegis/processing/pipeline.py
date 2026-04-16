"""Processing pipeline — LangGraph-based extraction workflow.

Flow: classify → [branch by source] → extract → resolve → store
Phase 2 wires up meeting extraction only. Email/chat added in Phase 3.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, StateGraph
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.engine import async_session_factory
from aegis.db.models import Meeting

logger = logging.getLogger(__name__)


class PipelineState(BaseModel):
    """State passed through the pipeline graph."""

    item_id: int
    item_type: str  # "meeting", "email", "chat_message"
    transcript_text: str = ""
    attendee_names: list[str] = []
    extraction_result: dict[str, Any] | None = None
    error: str | None = None


# ── Node functions ────────────────────────────────────────


async def classify_node(state: PipelineState) -> dict:
    """Determine source type — already known from item_type."""
    logger.info("Pipeline classify: item_type=%s, item_id=%d", state.item_type, state.item_id)
    return {}


async def extract_meeting_node(state: PipelineState) -> dict:
    """Run meeting extraction via Haiku."""
    from aegis.processing.meeting_extractor import extract_meeting

    try:
        async with async_session_factory() as session:
            result = await extract_meeting(
                session=session,
                meeting_id=state.item_id,
                transcript_text=state.transcript_text,
                attendee_names=state.attendee_names,
            )
            return {"extraction_result": result}
    except Exception as e:
        logger.exception("Meeting extraction failed for meeting %d", state.item_id)
        return {"error": str(e)}


async def resolve_node(state: PipelineState) -> dict:
    """Entity resolution — match extracted people against People table."""
    if state.error or not state.extraction_result:
        return {}

    from aegis.processing.resolver import resolve_extracted_entities

    try:
        async with async_session_factory() as session:
            # resolve modifies extraction in-place, adding _resolved_people
            extraction = state.extraction_result
            await resolve_extracted_entities(
                session=session,
                meeting_id=state.item_id,
                extraction=extraction,
            )
            # MUST return the modified extraction so LangGraph persists it
            return {"extraction_result": extraction}
    except Exception as e:
        logger.exception("Entity resolution failed for meeting %d", state.item_id)
        return {"error": str(e)}


async def store_node(state: PipelineState) -> dict:
    """Persist extracted entities to database + generate embeddings."""
    if state.error or not state.extraction_result:
        return {}

    from aegis.processing.meeting_extractor import store_meeting_extraction

    try:
        async with async_session_factory() as session:
            await store_meeting_extraction(
                session=session,
                meeting_id=state.item_id,
                extraction=state.extraction_result,
            )
            return {}
    except Exception as e:
        logger.exception("Store failed for meeting %d", state.item_id)
        return {"error": str(e)}


def route_by_type(state: PipelineState) -> str:
    """Branch to the correct extractor based on item_type."""
    if state.item_type == "meeting":
        return "extract_meeting"
    # Phase 3: email, chat_message
    return "end"


# ── Build the graph ───────────────────────────────────────


def build_pipeline() -> StateGraph:
    """Construct the LangGraph processing pipeline."""
    graph = StateGraph(PipelineState)

    graph.add_node("classify", classify_node)
    graph.add_node("extract_meeting", extract_meeting_node)
    graph.add_node("resolve", resolve_node)
    graph.add_node("store", store_node)

    graph.set_entry_point("classify")
    graph.add_conditional_edges("classify", route_by_type, {
        "extract_meeting": "extract_meeting",
        "end": END,
    })
    graph.add_edge("extract_meeting", "resolve")
    graph.add_edge("resolve", "store")
    graph.add_edge("store", END)

    return graph


_compiled_pipeline = None


def get_pipeline():
    """Get or create the compiled pipeline."""
    global _compiled_pipeline
    if _compiled_pipeline is None:
        _compiled_pipeline = build_pipeline().compile()
    return _compiled_pipeline


# ── Runner ────────────────────────────────────────────────


async def process_meeting(meeting_id: int) -> bool:
    """Run the full pipeline for a single meeting. Returns True on success."""
    async with async_session_factory() as session:
        meeting = await session.get(Meeting, meeting_id)
        if not meeting:
            logger.error("Meeting %d not found", meeting_id)
            return False

        if not meeting.transcript_text:
            logger.info("Meeting %d has no transcript, skipping", meeting_id)
            return False

        if meeting.last_extracted_at and meeting.processing_status == "completed":
            logger.info("Meeting %d already extracted, skipping", meeting_id)
            return True

        # Set processing status
        await session.execute(
            update(Meeting)
            .where(Meeting.id == meeting_id)
            .values(processing_status="processing")
        )
        await session.commit()

    # Get attendee names
    from aegis.db.repositories import get_meeting_attendees

    async with async_session_factory() as session:
        attendees = await get_meeting_attendees(session, meeting_id)
        attendee_names = [a.name for a in attendees]

    # Run pipeline
    pipeline = get_pipeline()
    initial_state = PipelineState(
        item_id=meeting_id,
        item_type="meeting",
        transcript_text=meeting.transcript_text,
        attendee_names=attendee_names,
    )

    try:
        final_state = await pipeline.ainvoke(initial_state)

        async with async_session_factory() as session:
            if final_state.get("error"):
                await session.execute(
                    update(Meeting)
                    .where(Meeting.id == meeting_id)
                    .values(
                        processing_status="failed",
                        processing_error=str(final_state["error"])[:500],
                    )
                )
            else:
                await session.execute(
                    update(Meeting)
                    .where(Meeting.id == meeting_id)
                    .values(
                        processing_status="completed",
                        last_extracted_at=datetime.now(timezone.utc),
                    )
                )
            await session.commit()

        return not final_state.get("error")

    except Exception as e:
        logger.exception("Pipeline failed for meeting %d", meeting_id)
        async with async_session_factory() as session:
            await session.execute(
                update(Meeting)
                .where(Meeting.id == meeting_id)
                .values(
                    processing_status="failed",
                    processing_error=str(e)[:500],
                )
            )
            await session.commit()
        return False


async def process_pending_meetings() -> int:
    """Find and process all meetings with transcripts that haven't been extracted yet."""
    from sqlalchemy import select

    async with async_session_factory() as session:
        stmt = (
            select(Meeting.id)
            .where(
                Meeting.transcript_text.isnot(None),
                Meeting.transcript_text != "",
                Meeting.processing_status.in_(["pending", "failed"]),
                Meeting.is_excluded.is_(False),
            )
            .order_by(Meeting.start_time)
        )
        result = await session.execute(stmt)
        meeting_ids = [row[0] for row in result.all()]

    count = 0
    for mid in meeting_ids:
        if await process_meeting(mid):
            count += 1

    logger.info("Processed %d/%d pending meetings", count, len(meeting_ids))
    return count
