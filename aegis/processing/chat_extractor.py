"""Chat message extraction via Anthropic Haiku — intent, asks, sentiment."""

import json
import logging
from datetime import date, datetime, timezone

from anthropic import AsyncAnthropic
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import ChatAsk, ChatMessage, ChatMessageTopic, LLMUsage
from aegis.processing.embeddings import embed_text

logger = logging.getLogger(__name__)

EXTRACTION_MODEL = "claude-haiku-4-5-20251001"


# ── Pydantic schemas ─────────────────────────────────────


class ExtractedChatAsk(BaseModel):
    ask_type: str  # deliverable/decision/follow_up/question/approval/review/info_request
    description: str
    requester: str | None = None  # person name
    target: str | None = None  # person name
    deadline: str | None = None
    urgency: str = "medium"  # high/medium/low


class ExtractedPerson(BaseModel):
    name: str
    email: str | None = None


class ChatExtraction(BaseModel):
    summary: str
    intent: str  # request/fyi/decision_needed/follow_up/question/response
    requires_response: bool
    asks: list[ExtractedChatAsk]
    people: list[ExtractedPerson]
    topics: list[str]
    sentiment: str  # positive/neutral/tense/negative/urgent


class ChannelBatchExtraction(BaseModel):
    summary: str
    asks: list[ExtractedChatAsk]
    topics: list[str]
    sentiment: str


CHAT_EXTRACTION_PROMPT = """\
You are an AI assistant that extracts structured information from Teams chat messages.
Chat messages are typically informal and short. Adapt your extraction accordingly.

Given the chat message below, extract:

1. **summary**: A single sentence summarizing the message.
2. **intent**: The primary intent — one of: request, fyi, decision_needed, follow_up, question, response.
3. **requires_response**: Whether the sender expects a reply (true/false).
4. **asks**: Any specific asks or requests. For each, identify:
   - ask_type: deliverable, decision, follow_up, question, approval, review, info_request
   - description: what is being asked
   - requester: who is asking (sender name)
   - target: who it is directed at (null if unclear)
   - deadline: any deadline mentioned (null if none)
   - urgency: high, medium, or low
5. **people**: People mentioned or involved. Include name and email if known.
6. **topics**: 1-5 topic keywords/phrases.
7. **sentiment**: Overall tone — one of: positive, neutral, tense, negative, urgent.

Sender: {sender_name}
{attachment_context}

Return ONLY valid JSON matching this schema (no markdown, no code blocks):
{{
  "summary": "string",
  "intent": "request|fyi|decision_needed|follow_up|question|response",
  "requires_response": true|false,
  "asks": [{{"ask_type": "string", "description": "string", "requester": "string or null", "target": "string or null", "deadline": "string or null", "urgency": "high|medium|low"}}],
  "people": [{{"name": "string", "email": "string or null"}}],
  "topics": ["string"],
  "sentiment": "positive|neutral|tense|negative|urgent"
}}

Message:
{message_text}
"""

CHANNEL_BATCH_PROMPT = """\
You are an AI assistant that extracts structured information from a batch of Teams channel messages.
These messages are from the same channel within a 30-minute window.

Summarize the batch as a whole. Extract any asks or action items from across all messages.

Messages:
{messages_text}

Return ONLY valid JSON (no markdown, no code blocks):
{{
  "summary": "string",
  "asks": [{{"ask_type": "string", "description": "string", "requester": "string or null", "target": "string or null", "deadline": "string or null", "urgency": "high|medium|low"}}],
  "topics": ["string"],
  "sentiment": "positive|neutral|tense|negative|urgent"
}}
"""


async def extract_chat(
    session: AsyncSession,
    message_id: int,
) -> dict:
    """Extract entities from a single chat message using Haiku.

    Returns a dict matching ChatExtraction schema.
    """
    msg = await session.get(ChatMessage, message_id)
    if not msg or not msg.body_text:
        return {}

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Resolve sender name
    sender_name = "Unknown"
    if msg.sender_id:
        from aegis.db.repositories import get_person_by_id

        person = await get_person_by_id(session, msg.sender_id)
        if person:
            sender_name = person.name

    # Build attachment context for prompt
    attachment_context = ""
    from aegis.db.models import Attachment

    att_result = await session.execute(
        select(Attachment).where(
            Attachment.source_type == "chat_message",
            Attachment.source_id == message_id,
            Attachment.is_inline == False,  # noqa: E712
        )
    )
    attachments = list(att_result.scalars().all())
    if attachments:
        filenames = [a.filename for a in attachments]
        attachment_context = f"Attachments: {', '.join(filenames)}"

    truncated_text = msg.body_text[:10_000]

    prompt = CHAT_EXTRACTION_PROMPT.format(
        sender_name=sender_name,
        attachment_context=attachment_context,
        message_text=truncated_text,
    )

    response = await client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=2048,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )

    content = response.content[0].text
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    raw = json.loads(content.strip())

    extraction = ChatExtraction(**raw)

    # Track LLM usage
    await _track_usage(
        session,
        task="chat_extraction",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

    logger.info(
        "Extracted chat message %d: intent=%s, %d asks",
        message_id,
        extraction.intent,
        len(extraction.asks),
    )

    return extraction.model_dump()


async def extract_channel_batch(
    session: AsyncSession,
    channel_id: int,
    window_start: datetime,
    window_end: datetime,
) -> dict:
    """Extract a batch summary for channel messages in a 30-min window.

    Returns a dict matching ChannelBatchExtraction schema.
    """
    stmt = (
        select(ChatMessage)
        .where(
            ChatMessage.channel_id == channel_id,
            ChatMessage.datetime_ >= window_start,
            ChatMessage.datetime_ < window_end,
            ChatMessage.noise_filtered == False,  # noqa: E712
        )
        .order_by(ChatMessage.datetime_)
    )
    result = await session.execute(stmt)
    messages = list(result.scalars().all())

    if not messages:
        return {}

    # Build combined text
    lines = []
    for m in messages:
        sender_label = f"[User {m.sender_id}]" if m.sender_id else "[Unknown]"
        lines.append(f"{sender_label}: {m.body_text or ''}")
    messages_text = "\n".join(lines)[:20_000]

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    prompt = CHANNEL_BATCH_PROMPT.format(messages_text=messages_text)

    response = await client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=2048,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )

    content = response.content[0].text
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    raw = json.loads(content.strip())

    extraction = ChannelBatchExtraction(**raw)

    await _track_usage(
        session,
        task="channel_batch_extraction",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

    logger.info(
        "Extracted channel batch (channel %d, %s to %s): %d messages, %d asks",
        channel_id,
        window_start.isoformat(),
        window_end.isoformat(),
        len(messages),
        len(extraction.asks),
    )

    return extraction.model_dump()


async def store_chat_extraction(
    session: AsyncSession,
    message_id: int,
    extraction: dict,
) -> None:
    """Persist extracted entities for a chat message.

    Delete-and-replace pattern for ChatAsk records. Idempotent.
    """
    from aegis.db.repositories import get_or_create_person_by_email, upsert_topic

    parsed = ChatExtraction(**extraction)

    # Delete existing asks for this message
    await session.execute(delete(ChatAsk).where(ChatAsk.message_id == message_id))
    await session.execute(
        delete(ChatMessageTopic).where(ChatMessageTopic.chat_message_id == message_id)
    )
    await session.flush()

    # Build name->person_id from resolved data
    resolved_people: dict[str, int] = extraction.get("_resolved_people", {})

    # Create ChatAsk records
    for ask in parsed.asks:
        requester_id = resolved_people.get(ask.requester) if ask.requester else None
        target_id = resolved_people.get(ask.target) if ask.target else None
        embedding = await embed_text(ask.description)

        chat_ask = ChatAsk(
            message_id=message_id,
            ask_type=ask.ask_type,
            description=ask.description,
            requester_id=requester_id,
            target_id=target_id,
            deadline=ask.deadline,
            urgency=ask.urgency,
            embedding=embedding,
        )
        session.add(chat_ask)

    # Link topics
    for topic_name in parsed.topics:
        topic = await upsert_topic(session, name=topic_name)
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(ChatMessageTopic).values(
            chat_message_id=message_id, topic_id=topic.id
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["chat_message_id", "topic_id"]
        )
        await session.execute(stmt)

    # Update the chat message record
    msg_embedding = await embed_text(parsed.summary)
    from sqlalchemy import update

    stmt = (
        update(ChatMessage)
        .where(ChatMessage.id == message_id)
        .values(
            intent=parsed.intent,
            requires_response=parsed.requires_response,
            summary=parsed.summary,
            sentiment=parsed.sentiment,
            embedding=msg_embedding,
            last_extracted_at=datetime.now(timezone.utc),
            processing_status="completed",
        )
    )
    await session.execute(stmt)
    await session.commit()

    logger.info("Stored extraction for chat message %d", message_id)


async def _track_usage(
    session: AsyncSession,
    task: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

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
    await session.commit()
