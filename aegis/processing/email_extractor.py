"""Email entity extraction via Anthropic Haiku — intent, asks, decisions, commitments."""

import json
import logging
from datetime import date, datetime, timezone

from anthropic import AsyncAnthropic
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import (
    Commitment,
    Decision,
    Email,
    EmailAsk,
    EmailTopic,
    LLMUsage,
)
from aegis.processing.embeddings import embed_text

logger = logging.getLogger(__name__)

EXTRACTION_MODEL = "claude-haiku-4-5-20251001"


# ── Pydantic schemas ───────────────────────────────────────


class ExtractedEmailAsk(BaseModel):
    description: str
    ask_type: str  # deliverable/decision/follow_up/question/approval/review/info_request
    requester_name: str
    target_name: str
    deadline: str | None = None
    urgency: str = "medium"  # high/medium/low


class ExtractedPerson(BaseModel):
    name: str
    role: str | None = None
    email: str | None = None


class ExtractedDecision(BaseModel):
    description: str
    decided_by: str


class ExtractedCommitment(BaseModel):
    description: str
    committer: str
    recipient: str | None = None
    deadline: str | None = None


class EmailExtraction(BaseModel):
    summary: str
    intent: str  # request/fyi/decision_needed/follow_up/question/response/scheduling
    requires_response: bool
    asks: list[ExtractedEmailAsk]
    people: list[ExtractedPerson]
    decisions_made: list[ExtractedDecision]
    commitments: list[ExtractedCommitment]
    topics: list[str]
    sentiment: str  # positive/neutral/tense/negative/urgent


EXTRACTION_PROMPT = """\
You are an AI assistant that extracts structured information from emails.

Given an email with sender, recipients, subject, and body, extract ALL of the following:

1. **summary**: A concise 1-2 sentence summary of the email.
2. **intent**: The primary intent — one of: request, fyi, decision_needed, follow_up, question, response, scheduling.
3. **requires_response**: true if this email needs a reply from the user, false otherwise.
4. **asks**: Every ask/request in the email. CRITICAL: identify the REQUESTER (who is asking) and the TARGET (who must act).
   - If the sender says "Can you send me the report?" → requester is the sender, target is the recipient.
   - If the sender says "I need John to review this" → requester is the sender, target is "John".
   - Each ask needs: description, ask_type (deliverable/decision/follow_up/question/approval/review/info_request), requester_name, target_name, deadline (if mentioned), urgency (high/medium/low).
5. **people**: Every person mentioned or involved. Include name, role (if mentioned), email (if known).
6. **decisions_made**: Any decisions announced in this email.
7. **commitments**: Promises made (e.g., "I'll send it by Thursday").
8. **topics**: 2-5 topic keywords.
9. **sentiment**: Overall tone — one of: positive, neutral, tense, negative, urgent.

{attachment_context}

Email details:
From: {sender_name} <{sender_email}>
To: {recipients}
Subject: {subject}
Date: {date}

Body:
{body}

Return ONLY valid JSON matching this exact schema (no markdown, no code blocks):
{{
  "summary": "string",
  "intent": "request|fyi|decision_needed|follow_up|question|response|scheduling",
  "requires_response": true/false,
  "asks": [{{"description": "string", "ask_type": "string", "requester_name": "string", "target_name": "string", "deadline": "string or null", "urgency": "high|medium|low"}}],
  "people": [{{"name": "string", "role": "string or null", "email": "string or null"}}],
  "decisions_made": [{{"description": "string", "decided_by": "string"}}],
  "commitments": [{{"description": "string", "committer": "string", "recipient": "string or null", "deadline": "string or null"}}],
  "topics": ["string"],
  "sentiment": "positive|neutral|tense|negative|urgent"
}}
"""


async def extract_email(session: AsyncSession, email_id: int) -> dict:
    """Extract structured entities from an email using Haiku.

    Returns a dict matching the EmailExtraction schema.
    """
    email = await session.get(Email, email_id)
    if not email:
        raise ValueError(f"Email {email_id} not found")

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Build sender info
    sender_name = ""
    sender_email = ""
    if email.sender_id:
        from aegis.db.repositories import get_person_by_id

        sender = await get_person_by_id(session, email.sender_id)
        if sender:
            sender_name = sender.name
            sender_email = sender.email or ""

    # Build recipients string
    recip_parts = []
    for r in (email.recipients or []):
        name = r.get("name", "")
        addr = r.get("email", "")
        rtype = r.get("type", "to")
        if name and addr:
            recip_parts.append(f"{name} <{addr}> ({rtype})")
        elif addr:
            recip_parts.append(f"{addr} ({rtype})")
    recipients_str = ", ".join(recip_parts) if recip_parts else "Unknown"

    # Build attachment context
    attachment_context = ""
    if email.has_attachments:
        from sqlalchemy import select as sa_select

        from aegis.db.models import Attachment

        att_stmt = sa_select(Attachment).where(
            Attachment.source_type == "email",
            Attachment.source_id == email_id,
            Attachment.is_inline == False,  # noqa: E712
        )
        att_result = await session.execute(att_stmt)
        attachments = list(att_result.scalars().all())
        if attachments:
            att_names = [a.filename for a in attachments]
            attachment_context = (
                f"This email has {len(attachments)} attachment(s): {', '.join(att_names)}. "
                "Consider attachment filenames when identifying asks and deliverables."
            )

    # Truncate body
    body_text = (email.body_text or email.body_preview or "")[:15_000]

    prompt = EXTRACTION_PROMPT.format(
        sender_name=sender_name,
        sender_email=sender_email,
        recipients=recipients_str,
        subject=email.subject or "(No Subject)",
        date=email.datetime_.isoformat() if email.datetime_ else "",
        body=body_text,
        attachment_context=attachment_context,
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
    extraction = EmailExtraction(**raw)

    # Track LLM usage
    await _track_usage(
        session,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

    logger.info(
        "Extracted email %d: intent=%s, %d asks, %d decisions",
        email_id,
        extraction.intent,
        len(extraction.asks),
        len(extraction.decisions_made),
    )

    return extraction.model_dump()


async def store_email_extraction(
    session: AsyncSession,
    email_id: int,
    extraction: dict,
) -> None:
    """Persist extracted entities to the database. Delete-and-replace pattern."""
    from aegis.db.repositories import (
        create_commitment,
        create_decision,
        get_or_create_person_by_email,
        link_email_topics,
        update_email_extraction,
        upsert_topic,
    )

    parsed = EmailExtraction(**extraction)

    # Fetch the email for thread_id
    email = await session.get(Email, email_id)
    thread_id = email.thread_id if email else None

    # ── Delete-and-replace existing entities for this email ──
    await session.execute(delete(EmailAsk).where(EmailAsk.email_id == email_id))
    await session.execute(delete(Decision).where(Decision.source_email_id == email_id))
    await session.execute(delete(Commitment).where(Commitment.source_email_id == email_id))
    await session.execute(delete(EmailTopic).where(EmailTopic.email_id == email_id))
    await session.flush()

    # Build name->person_id lookup from resolved data
    resolved_people: dict[str, int] = extraction.get("_resolved_people", {})

    # ── Email Asks ──────────────────────────────────────────
    for ask in parsed.asks:
        requester_id = resolved_people.get(ask.requester_name)
        target_id = resolved_people.get(ask.target_name)

        embedding = await embed_text(ask.description)
        email_ask = EmailAsk(
            email_id=email_id,
            thread_id=thread_id,
            ask_type=ask.ask_type,
            description=ask.description,
            requester_id=requester_id,
            target_id=target_id,
            deadline=ask.deadline,
            urgency=ask.urgency,
            status="open",
            embedding=embedding,
        )
        session.add(email_ask)

    # ── Decisions ───────────────────────────────────────────
    for dec in parsed.decisions_made:
        decided_by_id = resolved_people.get(dec.decided_by)
        embedding = await embed_text(dec.description)
        await create_decision(
            session,
            description=dec.description,
            decided_by=decided_by_id,
            source_email_id=email_id,
            embedding=embedding,
        )

    # ── Commitments ─────────────────────────────────────────
    for com in parsed.commitments:
        committer_id = resolved_people.get(com.committer)
        recipient_id = resolved_people.get(com.recipient) if com.recipient else None
        await create_commitment(
            session,
            description=com.description,
            committer_id=committer_id,
            recipient_id=recipient_id,
            source_email_id=email_id,
            deadline=com.deadline,
        )

    # ── Topics ──────────────────────────────────────────────
    topic_ids = []
    for topic_name in parsed.topics:
        topic = await upsert_topic(session, name=topic_name)
        topic_ids.append(topic.id)
    if topic_ids:
        await link_email_topics(session, email_id=email_id, topic_ids=topic_ids)

    # ── Update email record ─────────────────────────────────
    email_embedding = await embed_text(parsed.summary)
    await update_email_extraction(
        session,
        email_id=email_id,
        summary=parsed.summary,
        intent=parsed.intent,
        requires_response=parsed.requires_response,
        sentiment=parsed.sentiment,
        embedding=email_embedding,
    )

    await session.commit()
    logger.info("Stored extraction for email %d", email_id)


async def _track_usage(
    session: AsyncSession, input_tokens: int, output_tokens: int
) -> None:
    today = date.today()
    stmt = pg_insert(LLMUsage).values(
        date=today,
        model=EXTRACTION_MODEL,
        task="email_extraction",
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
