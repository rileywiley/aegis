"""RAG (Retrieval-Augmented Generation) chat system for Ask Aegis.

Intent classification -> structured query OR semantic search -> LLM answer with citations.
"""

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Literal

import anthropic
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import (
    ActionItem,
    ChatMessage,
    ChatSession,
    Decision,
    Email,
    EmailAsk,
    ChatAsk,
    LLMUsage,
    Meeting,
    Person,
)
from aegis.processing.embeddings import embed_text

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6-20250514"

INTENT_SYSTEM = """You classify user questions about workplace data into one of three categories.

Respond with ONLY a JSON object, no other text.

Categories:
- "structured": Questions that can be answered by counting, listing, or filtering database records.
  Examples: "how many emails today?", "list open action items", "who has the most asks?"
- "semantic": Questions requiring meaning-based search across meeting transcripts, emails, chat.
  Examples: "what did James say about the migration?", "summarize the budget discussion"
- "hybrid": Questions needing both structured filtering and semantic understanding.
  Examples: "what decisions were made in this week's meetings?", "any urgent asks about the launch?"

Output format: {"intent": "structured"|"semantic"|"hybrid", "entities": ["relevant names/topics"]}"""


async def _classify_intent(question: str) -> dict:
    """Use Haiku to classify the question intent."""
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    try:
        response = await client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=200,
            temperature=0,
            system=INTENT_SYSTEM,
            messages=[{"role": "user", "content": question}],
        )
        content = response.content[0].text.strip()
        # Track usage
        await _track_llm_usage(
            HAIKU_MODEL, "rag_classify",
            response.usage.input_tokens, response.usage.output_tokens,
        )
        return json.loads(content)
    except (json.JSONDecodeError, Exception):
        logger.exception("Intent classification failed, defaulting to semantic")
        return {"intent": "semantic", "entities": []}


async def _track_llm_usage(
    model: str, task: str, input_tokens: int, output_tokens: int
) -> None:
    """Record LLM usage — fire-and-forget style, errors swallowed."""
    try:
        from aegis.db.engine import async_session_factory
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        async with async_session_factory() as session:
            today = date.today()
            stmt = pg_insert(LLMUsage).values(
                date=today, model=model, task=task,
                input_tokens=input_tokens, output_tokens=output_tokens, calls=1,
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
    except Exception:
        logger.debug("Failed to track LLM usage", exc_info=True)


async def _run_structured_query(session: AsyncSession, question: str, entities: list[str]) -> list[dict]:
    """Execute structured SQL queries for counting/listing questions."""
    q_lower = question.lower()
    results: list[dict] = []

    now = datetime.now(timezone.utc)

    if any(word in q_lower for word in ["email", "emails"]):
        if "today" in q_lower:
            from datetime import timedelta
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            stmt = select(func.count()).select_from(Email).where(
                Email.datetime_ >= start
            )
            count = (await session.execute(stmt)).scalar_one()
            results.append({"type": "count", "label": "Emails today", "value": count})
        elif "unread" in q_lower:
            stmt = select(func.count()).select_from(Email).where(Email.is_read == False)  # noqa: E712
            count = (await session.execute(stmt)).scalar_one()
            results.append({"type": "count", "label": "Unread emails", "value": count})

    if any(word in q_lower for word in ["action item", "action items", "tasks"]):
        status = "open"
        if "completed" in q_lower:
            status = "completed"
        elif "stale" in q_lower:
            status = "stale"
        stmt = select(func.count()).select_from(ActionItem).where(ActionItem.status == status)
        count = (await session.execute(stmt)).scalar_one()
        results.append({"type": "count", "label": f"{status.title()} action items", "value": count})

        # Also fetch a few
        items_stmt = (
            select(ActionItem)
            .where(ActionItem.status == status)
            .order_by(ActionItem.created.desc())
            .limit(10)
        )
        items_result = await session.execute(items_stmt)
        for item in items_result.scalars().all():
            results.append({
                "type": "action_item",
                "id": item.id,
                "description": item.description,
                "status": item.status,
                "deadline": item.deadline,
            })

    if any(word in q_lower for word in ["ask", "asks", "pending asks"]):
        stmt = select(func.count()).select_from(EmailAsk).where(EmailAsk.status == "open")
        ea_count = (await session.execute(stmt)).scalar_one()
        stmt = select(func.count()).select_from(ChatAsk).where(ChatAsk.status == "open")
        ca_count = (await session.execute(stmt)).scalar_one()
        results.append({"type": "count", "label": "Open asks (email)", "value": ea_count})
        results.append({"type": "count", "label": "Open asks (chat)", "value": ca_count})

    if any(word in q_lower for word in ["meeting", "meetings"]):
        if "today" in q_lower:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            from datetime import timedelta
            end = start + timedelta(days=1)
            stmt = (
                select(Meeting)
                .where(Meeting.start_time >= start, Meeting.start_time < end)
                .order_by(Meeting.start_time)
                .limit(20)
            )
            items_result = await session.execute(stmt)
            for m in items_result.scalars().all():
                results.append({
                    "type": "meeting",
                    "id": m.id,
                    "title": m.title,
                    "start_time": m.start_time.isoformat() if m.start_time else None,
                    "status": m.status,
                })

    if any(word in q_lower for word in ["decision", "decisions"]):
        stmt = (
            select(Decision)
            .order_by(Decision.datetime_.desc())
            .limit(10)
        )
        items_result = await session.execute(stmt)
        for d in items_result.scalars().all():
            results.append({
                "type": "decision",
                "id": d.id,
                "description": d.description,
                "datetime": d.datetime_.isoformat() if d.datetime_ else None,
            })

    if not results:
        # Fallback: count some basics
        for model, label in [
            (Meeting, "Total meetings"),
            (Email, "Total emails"),
            (ActionItem, "Total action items"),
            (Person, "Total people"),
        ]:
            count = (await session.execute(select(func.count()).select_from(model))).scalar_one()
            results.append({"type": "count", "label": label, "value": count})

    return results


async def _semantic_search(
    session: AsyncSession, question: str, limit: int = 15
) -> list[dict]:
    """Vector similarity search across meetings, emails, and chat messages.

    Ranking: similarity * 0.5 + recency * 0.2 + triage_weight * 0.3
    Only searches substantive + contextual items (excludes noise).
    """
    query_embedding = await embed_text(question)
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    # Search meetings
    meeting_sql = text("""
        SELECT id, title AS label, summary AS content, start_time AS dt,
               'meeting' AS source_type,
               1 - (embedding <=> :query_embedding::vector) AS similarity,
               1.0 AS triage_weight
        FROM meetings
        WHERE embedding IS NOT NULL
          AND processing_status = 'completed'
        ORDER BY embedding <=> :query_embedding::vector
        LIMIT :limit
    """)

    # Search emails (substantive + contextual only)
    email_sql = text("""
        SELECT id, subject AS label, summary AS content, datetime AS dt,
               'email' AS source_type,
               1 - (embedding <=> :query_embedding::vector) AS similarity,
               CASE WHEN triage_class = 'substantive' THEN 1.0
                    WHEN triage_class = 'contextual' THEN 0.5
                    ELSE 0.2 END AS triage_weight
        FROM emails
        WHERE embedding IS NOT NULL
          AND triage_class IN ('substantive', 'contextual')
        ORDER BY embedding <=> :query_embedding::vector
        LIMIT :limit
    """)

    # Search chat messages (substantive + contextual only)
    chat_sql = text("""
        SELECT id, summary AS label, body_text AS content, datetime AS dt,
               'chat_message' AS source_type,
               1 - (embedding <=> :query_embedding::vector) AS similarity,
               CASE WHEN triage_class = 'substantive' THEN 1.0
                    WHEN triage_class = 'contextual' THEN 0.5
                    ELSE 0.2 END AS triage_weight
        FROM chat_messages
        WHERE embedding IS NOT NULL
          AND triage_class IN ('substantive', 'contextual')
        ORDER BY embedding <=> :query_embedding::vector
        LIMIT :limit
    """)

    params = {"query_embedding": embedding_str, "limit": limit}
    all_results: list[dict] = []

    for sql in [meeting_sql, email_sql, chat_sql]:
        try:
            result = await session.execute(sql, params)
            for row in result.mappings().all():
                all_results.append(dict(row))
        except Exception:
            logger.debug("Semantic search query failed", exc_info=True)

    # Compute composite score: similarity * 0.5 + recency * 0.2 + triage_weight * 0.3
    now = datetime.now(timezone.utc)
    for item in all_results:
        sim = float(item.get("similarity") or 0)
        triage_w = float(item.get("triage_weight") or 0.2)
        dt = item.get("dt")
        if dt and hasattr(dt, "timestamp"):
            age_days = max((now - dt.replace(tzinfo=timezone.utc if dt.tzinfo is None else dt.tzinfo)).days, 0)
            recency = max(0, 1.0 - (age_days / 365.0))
        else:
            recency = 0.0
        item["composite_score"] = sim * 0.5 + recency * 0.2 + triage_w * 0.3

    all_results.sort(key=lambda x: x["composite_score"], reverse=True)
    return all_results[:limit]


async def _generate_answer(
    question: str,
    context: list[dict],
    conversation_history: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Use Sonnet to generate a sourced answer from retrieved context."""
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Build context block
    context_parts = []
    sources: list[dict] = []
    for i, item in enumerate(context[:8]):
        source_type = item.get("source_type", "unknown")
        label = item.get("label") or item.get("description") or "Untitled"
        content = item.get("content") or item.get("value") or ""
        dt = item.get("dt") or item.get("datetime") or ""
        item_id = item.get("id")

        if isinstance(dt, datetime):
            dt = dt.strftime("%Y-%m-%d %H:%M")
        elif isinstance(dt, str) and "T" in dt:
            dt = dt[:16].replace("T", " ")

        source_ref = f"[{i+1}]"
        context_parts.append(
            f"{source_ref} ({source_type}) {label} ({dt})\n{str(content)[:1000]}"
        )
        sources.append({
            "ref": source_ref,
            "source_type": source_type,
            "label": str(label),
            "id": item_id,
            "url": _build_source_url(source_type, item_id),
        })

    context_text = "\n\n---\n\n".join(context_parts) if context_parts else "No relevant context found."

    system_prompt = (
        "You are Aegis, an AI Chief of Staff. Answer the user's question using "
        "ONLY the provided context. Cite sources using [N] notation. If the context "
        "does not contain enough information, say so honestly. Be concise and direct. "
        "Format your response in plain text with citations inline."
    )

    messages: list[dict] = []
    if conversation_history:
        messages.extend(conversation_history[-6:])  # Last 3 exchanges

    messages.append({
        "role": "user",
        "content": f"Context:\n{context_text}\n\nQuestion: {question}",
    })

    try:
        response = await client.messages.create(
            model=SONNET_MODEL,
            max_tokens=1500,
            temperature=0.3,
            system=system_prompt,
            messages=messages,
        )
        answer = response.content[0].text
        await _track_llm_usage(
            SONNET_MODEL, "rag_answer",
            response.usage.input_tokens, response.usage.output_tokens,
        )
        return answer, sources
    except Exception:
        logger.exception("RAG answer generation failed")
        return "I was unable to generate an answer. Please try again.", sources


def _build_source_url(source_type: str, item_id: int | None) -> str | None:
    """Build a clickable URL for a source item."""
    if item_id is None:
        return None
    if source_type == "meeting":
        return f"/meetings/{item_id}"
    elif source_type == "email":
        return f"/emails/{item_id}"
    elif source_type == "chat_message":
        return None  # No dedicated chat message page
    elif source_type == "action_item":
        return f"/actions?highlight={item_id}"
    elif source_type == "decision":
        return None
    return None


async def _get_or_create_session(
    session: AsyncSession, session_id: int | None
) -> tuple[ChatSession, list[dict]]:
    """Load an existing chat session or create a new one."""
    if session_id:
        chat_session = await session.get(ChatSession, session_id)
        if chat_session:
            messages = chat_session.messages if isinstance(chat_session.messages, list) else []
            return chat_session, messages

    # Create new session
    chat_session = ChatSession(messages=[], last_active=datetime.now(timezone.utc))
    session.add(chat_session)
    await session.flush()
    return chat_session, []


async def ask_aegis(
    session: AsyncSession,
    question: str,
    session_id: int | None = None,
) -> dict:
    """Main RAG entry point.

    Returns: {answer: str, sources: list[dict], session_id: int}
    """
    # Step 0: Load/create chat session
    chat_session, conversation_history = await _get_or_create_session(session, session_id)

    # Step 1: Classify intent
    classification = await _classify_intent(question)
    intent = classification.get("intent", "semantic")
    entities = classification.get("entities", [])

    # Step 2: Retrieve context based on intent
    context: list[dict] = []

    if intent == "structured":
        context = await _run_structured_query(session, question, entities)
    elif intent == "semantic":
        context = await _semantic_search(session, question)
    else:  # hybrid
        structured = await _run_structured_query(session, question, entities)
        semantic = await _semantic_search(session, question, limit=10)
        context = structured + semantic

    # Step 3: Generate answer with Sonnet
    # Convert conversation_history to Anthropic message format
    api_history = []
    for msg in conversation_history:
        if msg.get("role") in ("user", "assistant"):
            api_history.append({"role": msg["role"], "content": msg["content"]})

    answer, sources = await _generate_answer(question, context, api_history)

    # Step 4: Save to chat session
    conversation_history.append({"role": "user", "content": question})
    conversation_history.append({
        "role": "assistant",
        "content": answer,
        "sources": sources,
    })
    chat_session.messages = conversation_history
    chat_session.last_active = datetime.now(timezone.utc)
    await session.commit()

    return {
        "answer": answer,
        "sources": sources,
        "session_id": chat_session.id,
    }
