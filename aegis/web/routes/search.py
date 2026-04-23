"""Hybrid search page — keyword + semantic search across meetings, emails, chats."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import ChatMessage, Email, Meeting
from aegis.processing.embeddings import embed_text
from aegis.web import templates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/search")
async def search_page(request: Request):
    return templates.TemplateResponse(
        request,
        "search.html",
        {"current_time": "", "results": [], "query": "", "source_filter": "all"},
    )


@router.get("/search/results")
async def search_results(
    request: Request,
    q: str = Query("", alias="q"),
    source: str = Query("all"),
    session: AsyncSession = Depends(get_session),
):
    """HTMX partial — run hybrid search and return results fragment."""
    query = q.strip()
    if not query:
        return templates.TemplateResponse(
            request,
            "components/search_results.html",
            {"results": [], "query": ""},
        )

    results: list[dict] = []

    # ── Keyword search (ILIKE) ──────────────────────────────
    if source in ("all", "meetings"):
        kw_meetings = await _keyword_search_meetings(session, query)
        results.extend(kw_meetings)

    if source in ("all", "emails"):
        kw_emails = await _keyword_search_emails(session, query)
        results.extend(kw_emails)

    if source in ("all", "chats"):
        kw_chats = await _keyword_search_chats(session, query)
        results.extend(kw_chats)

    # ── Semantic search (pgvector) ──────────────────────────
    try:
        sem_results = await _semantic_search(session, query, source)
        results.extend(sem_results)
    except Exception:
        logger.debug("Semantic search failed — embeddings may not be available", exc_info=True)

    # ── Deduplicate + merge scores ──────────────────────────
    merged = _deduplicate_results(results)

    # Sort by composite score descending
    merged.sort(key=lambda x: x.get("score", 0), reverse=True)

    return templates.TemplateResponse(
        request,
        "components/search_results.html",
        {"results": merged[:50], "query": query},
    )


async def _keyword_search_meetings(
    session: AsyncSession, query: str, limit: int = 20,
) -> list[dict]:
    pattern = f"%{query}%"
    stmt = (
        select(Meeting)
        .where(
            or_(
                Meeting.title.ilike(pattern),
                Meeting.summary.ilike(pattern),
                Meeting.transcript_text.ilike(pattern),
            )
        )
        .order_by(Meeting.start_time.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    items = []
    for m in result.scalars().all():
        preview = m.summary or (m.transcript_text or "")[:200]
        items.append({
            "id": m.id,
            "source_type": "meeting",
            "title": m.title or "Untitled Meeting",
            "preview": preview[:200],
            "date": m.start_time.isoformat() if m.start_time else None,
            "url": f"/meetings/{m.id}",
            "score": 0.6,  # keyword match base score
            "method": "keyword",
        })
    return items


async def _keyword_search_emails(
    session: AsyncSession, query: str, limit: int = 20,
) -> list[dict]:
    pattern = f"%{query}%"
    stmt = (
        select(Email)
        .where(
            or_(
                Email.subject.ilike(pattern),
                Email.body_text.ilike(pattern),
                Email.summary.ilike(pattern),
            )
        )
        .order_by(Email.datetime_.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    items = []
    for e in result.scalars().all():
        preview = e.summary or e.body_preview or (e.body_text or "")[:200]
        items.append({
            "id": e.id,
            "source_type": "email",
            "title": e.subject or "No Subject",
            "preview": preview[:200],
            "date": e.datetime_.isoformat() if e.datetime_ else None,
            "url": f"/emails/{e.id}",
            "score": 0.6,
            "method": "keyword",
        })
    return items


async def _keyword_search_chats(
    session: AsyncSession, query: str, limit: int = 20,
) -> list[dict]:
    pattern = f"%{query}%"
    stmt = (
        select(ChatMessage)
        .where(
            or_(
                ChatMessage.body_text.ilike(pattern),
                ChatMessage.summary.ilike(pattern),
            )
        )
        .order_by(ChatMessage.datetime_.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    items = []
    for c in result.scalars().all():
        preview = c.summary or (c.body_text or "")[:200]
        items.append({
            "id": c.id,
            "source_type": "chat",
            "title": c.summary or (c.body_text or "")[:60],
            "preview": preview[:200],
            "date": c.datetime_.isoformat() if c.datetime_ else None,
            "url": None,  # No dedicated chat detail page
            "score": 0.6,
            "method": "keyword",
        })
    return items


async def _semantic_search(
    session: AsyncSession, query: str, source_filter: str, limit: int = 15,
) -> list[dict]:
    """Vector similarity search using pgvector CAST(:param AS vector) pattern."""
    query_embedding = await embed_text(query)
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    results: list[dict] = []
    params = {"query_embedding": embedding_str, "limit": limit}

    if source_filter in ("all", "meetings"):
        sql = text("""
            SELECT id, title, summary AS preview, start_time AS dt,
                   'meeting' AS source_type,
                   1 - (embedding <=> CAST(:query_embedding AS vector)) AS similarity
            FROM meetings
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:query_embedding AS vector)
            LIMIT :limit
        """)
        try:
            res = await session.execute(sql, params)
            for row in res.mappings().all():
                results.append(_row_to_result(row, "/meetings/"))
        except Exception:
            logger.debug("Semantic search on meetings failed", exc_info=True)
            await session.rollback()

    if source_filter in ("all", "emails"):
        sql = text("""
            SELECT id, subject AS title, summary AS preview, datetime AS dt,
                   'email' AS source_type,
                   1 - (embedding <=> CAST(:query_embedding AS vector)) AS similarity
            FROM emails
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:query_embedding AS vector)
            LIMIT :limit
        """)
        try:
            res = await session.execute(sql, params)
            for row in res.mappings().all():
                results.append(_row_to_result(row, "/emails/"))
        except Exception:
            logger.debug("Semantic search on emails failed", exc_info=True)
            await session.rollback()

    if source_filter in ("all", "chats"):
        sql = text("""
            SELECT id, summary AS title, body_text AS preview, datetime AS dt,
                   'chat' AS source_type,
                   1 - (embedding <=> CAST(:query_embedding AS vector)) AS similarity
            FROM chat_messages
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:query_embedding AS vector)
            LIMIT :limit
        """)
        try:
            res = await session.execute(sql, params)
            for row in res.mappings().all():
                results.append(_row_to_result(row, None))
        except Exception:
            logger.debug("Semantic search on chat_messages failed", exc_info=True)
            await session.rollback()

    return results


def _row_to_result(row: dict, url_prefix: str | None) -> dict:
    item_id = row.get("id")
    dt = row.get("dt")
    similarity = float(row.get("similarity") or 0)

    # Recency boost
    now = datetime.now(timezone.utc)
    recency = 0.0
    if dt and hasattr(dt, "timestamp"):
        dt_aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        age_days = max((now - dt_aware).days, 0)
        recency = max(0, 1.0 - (age_days / 365.0))

    score = similarity * 0.7 + recency * 0.3

    url = f"{url_prefix}{item_id}" if url_prefix and item_id else None

    return {
        "id": item_id,
        "source_type": row.get("source_type", "unknown"),
        "title": row.get("title") or "Untitled",
        "preview": (str(row.get("preview") or ""))[:200],
        "date": dt.isoformat() if dt and hasattr(dt, "isoformat") else str(dt) if dt else None,
        "url": url,
        "score": round(score, 3),
        "method": "semantic",
    }


def _deduplicate_results(results: list[dict]) -> list[dict]:
    """Merge duplicate results (same source_type + id), keeping highest score."""
    seen: dict[str, dict] = {}
    for r in results:
        dedup_key = f"{r.get('source_type')}:{r.get('id')}"
        if dedup_key in seen:
            existing = seen[dedup_key]
            if r.get("score", 0) > existing.get("score", 0):
                # Keep the higher-scored version but note both methods matched
                r["method"] = "hybrid"
                r["score"] = max(r.get("score", 0), existing.get("score", 0)) * 1.1  # boost
                seen[dedup_key] = r
            else:
                existing["method"] = "hybrid"
                existing["score"] = max(r.get("score", 0), existing.get("score", 0)) * 1.1
        else:
            seen[dedup_key] = r
    return list(seen.values())
