"""Meeting transcript extraction via Anthropic Haiku — structured entity extraction."""

import json
import logging
from datetime import date, datetime, timezone

from anthropic import AsyncAnthropic
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import LLMUsage
from aegis.processing.embeddings import embed_text

logger = logging.getLogger(__name__)

EXTRACTION_MODEL = "claude-haiku-4-5-20251001"


# ── Pydantic schemas for extraction output ───────────────


class ExtractedPerson(BaseModel):
    name: str
    role: str | None = None
    email: str | None = None


class ExtractedActionItem(BaseModel):
    description: str
    assignee: str | None = None  # person name
    deadline: str | None = None


class ExtractedDecision(BaseModel):
    description: str
    decided_by: str | None = None


class ExtractedCommitment(BaseModel):
    description: str
    committer: str | None = None
    recipient: str | None = None
    deadline: str | None = None


class MeetingExtraction(BaseModel):
    summary: str
    people: list[ExtractedPerson]
    action_items: list[ExtractedActionItem]
    decisions: list[ExtractedDecision]
    commitments: list[ExtractedCommitment]
    topics: list[str]
    sentiment: str  # positive/neutral/tense/negative/urgent


EXTRACTION_PROMPT = """\
You are an AI assistant that extracts structured information from meeting transcripts.
Given a transcript and a list of known attendees, extract ALL of the following:

1. **summary**: A concise 2-3 sentence summary of the meeting.
2. **people**: Every person mentioned or participating. Include name, role (if mentioned), and email (if mentioned).
3. **action_items**: Tasks assigned during the meeting. Include description, assignee name, and deadline if mentioned.
4. **decisions**: Decisions made during the meeting. Include description and who decided.
5. **commitments**: Promises made by one person to another. Include description, committer name, recipient name, and deadline if mentioned.
6. **topics**: A list of topic keywords/phrases discussed (3-8 topics).
7. **sentiment**: Overall meeting sentiment — one of: positive, neutral, tense, negative, urgent.

Known attendees: {attendees}

Return ONLY valid JSON matching this exact schema (no markdown, no code blocks):
{{
  "summary": "string",
  "people": [{{"name": "string", "role": "string or null", "email": "string or null"}}],
  "action_items": [{{"description": "string", "assignee": "string or null", "deadline": "string or null"}}],
  "decisions": [{{"description": "string", "decided_by": "string or null"}}],
  "commitments": [{{"description": "string", "committer": "string or null", "recipient": "string or null", "deadline": "string or null"}}],
  "topics": ["string"],
  "sentiment": "positive|neutral|tense|negative|urgent"
}}

Transcript:
{transcript}
"""


async def extract_meeting(
    session: AsyncSession,
    meeting_id: int,
    transcript_text: str,
    attendee_names: list[str],
) -> dict:
    """Extract entities from a meeting transcript using Haiku.

    Returns a dict matching the MeetingExtraction schema. Called from
    pipeline.py extract_meeting_node.
    """
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    attendees_str = ", ".join(attendee_names) if attendee_names else "Unknown"
    # Truncate very long transcripts to stay within context limits
    truncated_transcript = transcript_text[:30_000]

    prompt = EXTRACTION_PROMPT.format(
        attendees=attendees_str,
        transcript=truncated_transcript,
    )

    response = await client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=4096,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )

    content = response.content[0].text

    # Parse JSON — handle potential markdown wrapping
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    raw = json.loads(content.strip())

    # Validate through Pydantic
    extraction = MeetingExtraction(**raw)

    # Track LLM usage
    await _track_usage(
        session,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

    logger.info(
        "Extracted meeting %d: %d people, %d actions, %d decisions, %d commitments",
        meeting_id,
        len(extraction.people),
        len(extraction.action_items),
        len(extraction.decisions),
        len(extraction.commitments),
    )

    return extraction.model_dump()


async def store_meeting_extraction(
    session: AsyncSession,
    meeting_id: int,
    extraction: dict,
) -> None:
    """Persist extracted entities to the database and generate embeddings.

    Called from pipeline.py store_node. Idempotent — checks last_extracted_at
    before creating duplicate entities.
    """
    from aegis.db.repositories import (
        create_action_item,
        create_commitment,
        create_decision,
        link_meeting_topics,
        update_meeting_extraction,
        upsert_topic,
    )

    parsed = MeetingExtraction(**extraction)

    # Build a name->person_id lookup from the resolved extraction data.
    # resolve_node populates _resolved_people on the extraction dict.
    resolved_people: dict[str, int] = extraction.get("_resolved_people", {})

    # ── Action items ─────────────────────────────────────
    for item in parsed.action_items:
        assignee_id = resolved_people.get(item.assignee) if item.assignee else None
        embedding = await embed_text(item.description)
        await create_action_item(
            session,
            description=item.description,
            assignee_id=assignee_id,
            source_meeting_id=meeting_id,
            deadline=item.deadline,
            embedding=embedding,
        )

    # ── Decisions ────────────────────────────────────────
    for dec in parsed.decisions:
        decided_by_id = resolved_people.get(dec.decided_by) if dec.decided_by else None
        embedding = await embed_text(dec.description)
        await create_decision(
            session,
            description=dec.description,
            decided_by=decided_by_id,
            source_meeting_id=meeting_id,
            embedding=embedding,
        )

    # ── Commitments ──────────────────────────────────────
    for com in parsed.commitments:
        committer_id = resolved_people.get(com.committer) if com.committer else None
        recipient_id = resolved_people.get(com.recipient) if com.recipient else None
        await create_commitment(
            session,
            description=com.description,
            committer_id=committer_id,
            recipient_id=recipient_id,
            source_meeting_id=meeting_id,
            deadline=com.deadline,
        )

    # ── Topics ───────────────────────────────────────────
    topic_ids = []
    for topic_name in parsed.topics:
        topic = await upsert_topic(session, name=topic_name)
        topic_ids.append(topic.id)
    if topic_ids:
        await link_meeting_topics(session, meeting_id=meeting_id, topic_ids=topic_ids)

    # ── Update meeting record ────────────────────────────
    meeting_embedding = await embed_text(parsed.summary)
    await update_meeting_extraction(
        session,
        meeting_id=meeting_id,
        summary=parsed.summary,
        sentiment=parsed.sentiment,
        embedding=meeting_embedding,
    )

    await session.commit()

    logger.info("Stored extraction for meeting %d", meeting_id)


async def _track_usage(
    session: AsyncSession, input_tokens: int, output_tokens: int
) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    today = date.today()
    stmt = pg_insert(LLMUsage).values(
        date=today,
        model=EXTRACTION_MODEL,
        task="meeting_extraction",
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
    await session.commit()
