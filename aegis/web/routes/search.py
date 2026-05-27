"""Hybrid search page — keyword + semantic search across meetings, emails, chats.

This module also exposes ``GET /api/search`` — a JSON endpoint used by the
Helios menu bar app's manual attachment picker (HELIOS_BUILD_PLAN.md
Track 4H.2). The JSON variant searches across People, Workstreams, and
asks (Email + Chat) and returns a flat array. It lives here, alongside
the HTMX search page, because both endpoints share keyword-search
patterns; the JSON variant uses a separate handler because it has a
different response shape and result types (entities vs. content items).
"""

import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import text, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.engine import get_session
from aegis.db.models import ChatAsk, ChatMessage, Email, EmailAsk, Meeting, Person, Workstream
from aegis.processing.embeddings import embed_text
from aegis.web import templates

logger = logging.getLogger(__name__)

router = APIRouter()


# ═══════════════════════════════════════════════════════════
# JSON entity-search endpoint (Helios attachment picker)
# ═══════════════════════════════════════════════════════════


_VALID_TYPES = {"person", "workstream", "ask"}


class SearchResult(BaseModel):
    """One row of the /api/search response."""

    type: Literal["person", "workstream", "ask"]
    id: int
    label: str
    snippet: str


def _parse_types(types: str) -> set[str]:
    """Parse comma-separated types, drop unknowns, dedup. Empty → empty set."""
    if not types:
        return set()
    parts = {t.strip().lower() for t in types.split(",") if t.strip()}
    return parts & _VALID_TYPES


def _sort_key(query_lower: str, label: str) -> tuple[int, int, str]:
    """Rank exact prefix > exact substring > everything else, then by label."""
    label_lower = (label or "").lower()
    if not query_lower:
        return (2, 0, label_lower)
    if label_lower.startswith(query_lower):
        return (0, 0, label_lower)
    if query_lower in label_lower:
        return (1, label_lower.find(query_lower), label_lower)
    return (2, 0, label_lower)


async def _search_people(
    session: AsyncSession, query: str, limit: int,
) -> list[SearchResult]:
    pattern = f"%{query}%"
    # Match name OR email; aliases is an array column — use ARRAY contains
    # via a separate predicate. SQLAlchemy's any_/all_ patterns are
    # awkward for "any element ILIKE pattern", so we match name + email
    # only (good enough for v1 — the resolver does the heavy fuzzy work).
    stmt = (
        select(Person)
        .where(
            or_(
                Person.name.ilike(pattern),
                Person.email.ilike(pattern),
            )
        )
        .order_by(Person.name)
        .limit(limit)
    )
    result = await session.execute(stmt)
    items: list[SearchResult] = []
    for p in result.scalars().all():
        # Snippet: prefer title; fall back to role; then email; then "".
        snippet = p.title or p.role or p.email or ""
        items.append(
            SearchResult(
                type="person",
                id=p.id,
                label=p.name or (p.email or f"Person #{p.id}"),
                snippet=snippet,
            )
        )
    return items


async def _search_workstreams(
    session: AsyncSession, query: str, limit: int,
) -> list[SearchResult]:
    pattern = f"%{query}%"
    stmt = (
        select(Workstream)
        .where(
            or_(
                Workstream.name.ilike(pattern),
                Workstream.description.ilike(pattern),
            )
        )
        .order_by(Workstream.updated.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    items: list[SearchResult] = []
    for w in result.scalars().all():
        status = (w.status or "active").capitalize()
        desc = (w.description or "").strip()
        if desc:
            snippet = f"{status} · {desc[:80]}"
        else:
            snippet = status
        items.append(
            SearchResult(
                type="workstream",
                id=w.id,
                label=w.name or f"Workstream #{w.id}",
                snippet=snippet,
            )
        )
    return items


async def _search_asks(
    session: AsyncSession, query: str, limit: int,
) -> list[SearchResult]:
    """Search EmailAsk and ChatAsk by description; return as type='ask'.

    Each ask exposes its source via a marker prefix on the snippet so the
    user can disambiguate ("from email/chat: ..."). IDs are returned as-is
    from the source table; the caller distinguishes by source ONLY in the
    snippet — both source IDs are addressable separately by the wider Aegis
    data model. For the attachment picker that's fine: the picker just
    needs (type, id).
    """
    pattern = f"%{query}%"

    email_stmt = (
        select(EmailAsk, Email)
        .join(Email, EmailAsk.email_id == Email.id)
        .where(EmailAsk.description.ilike(pattern))
        .order_by(EmailAsk.updated.desc())
        .limit(limit)
    )
    chat_stmt = (
        select(ChatAsk, ChatMessage, Person.name.label("sender_name"))
        .join(ChatMessage, ChatAsk.message_id == ChatMessage.id)
        .outerjoin(Person, ChatMessage.sender_id == Person.id)
        .where(ChatAsk.description.ilike(pattern))
        .order_by(ChatAsk.updated.desc())
        .limit(limit)
    )

    items: list[SearchResult] = []

    email_res = await session.execute(email_stmt)
    for row in email_res.all():
        ask = row[0]
        email = row[1]
        # Try to surface a sender hint. Email senders are tracked via
        # Email.sender_id (people row). To keep this lean we fall back to
        # the Email.recipients JSON or just to "email".
        snippet = "from email"
        if email and getattr(email, "subject", None):
            snippet = f"from email: {email.subject[:60]}"
        items.append(
            SearchResult(
                type="ask",
                id=ask.id,
                label=ask.description[:120] if ask.description else f"Ask #{ask.id}",
                snippet=snippet,
            )
        )

    chat_res = await session.execute(chat_stmt)
    for row in chat_res.all():
        ask = row[0]
        sender_name = row[2] or "chat"
        snippet = f"from {sender_name} (Teams)"
        items.append(
            SearchResult(
                type="ask",
                id=ask.id,
                label=ask.description[:120] if ask.description else f"Ask #{ask.id}",
                snippet=snippet,
            )
        )

    return items


@router.get("/api/search", response_model=list[SearchResult])
async def api_search(
    q: str = Query("", description="Query string. Empty/whitespace → []."),
    types: str = Query(
        "person,workstream,ask",
        description="Comma-separated entity types: person, workstream, ask.",
    ),
    limit: int = Query(10, description="Max results across all types. Clamped to [1,50]."),
    session: AsyncSession = Depends(get_session),
) -> list[SearchResult]:
    """JSON entity search for the Helios attachment picker.

    Returns at most ``limit`` results across all requested types,
    keyword-matched (ILIKE) against name/email/description fields. Empty
    or whitespace-only ``q`` returns ``[]``. Invalid type values are
    silently filtered; if all types are invalid, returns ``[]``.

    The JSON variant lives on the same router as the existing HTMX
    ``/search`` page (which is included in ``aegis/main.py`` without a
    prefix), so this handler just declares its full path explicitly.
    """
    query = (q or "").strip()
    if not query:
        return []

    # Clamp limit to [1, 50]. ``int(limit) if limit is not None else 10``
    # — falsy values (0, negative) clamp to 1, not the default 10.
    try:
        raw_limit = int(limit) if limit is not None else 10
    except (TypeError, ValueError):
        raw_limit = 10
    capped_limit = max(1, min(raw_limit, 50))

    valid = _parse_types(types or "")
    if not valid:
        return []

    # Each per-type query gets the full limit budget; we cap the merged
    # output afterward. This keeps individual type results balanced when a
    # query matches a lot of one kind.
    per_type_limit = capped_limit

    results: list[SearchResult] = []

    if "person" in valid:
        results.extend(await _search_people(session, query, per_type_limit))
    if "workstream" in valid:
        results.extend(await _search_workstreams(session, query, per_type_limit))
    if "ask" in valid:
        results.extend(await _search_asks(session, query, per_type_limit))

    # Rank: prefix match > substring match > other; tie-break by label.
    query_lower = query.lower()
    results.sort(key=lambda r: _sort_key(query_lower, r.label))

    return results[:capped_limit]


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
            "url": f"/meetings/{m.id}?from=/search",
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
            "url": f"/emails/{e.id}?from=/search",
            "score": 0.6,
            "method": "keyword",
        })
    return items


async def _keyword_search_chats(
    session: AsyncSession, query: str, limit: int = 20,
) -> list[dict]:
    pattern = f"%{query}%"
    stmt = (
        select(ChatMessage, Person.name.label("sender_name"))
        .outerjoin(Person, ChatMessage.sender_id == Person.id)
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
    for row in result.all():
        c = row[0]
        sender_name = row[1] or "Unknown"
        preview = c.summary or (c.body_text or "")[:200]
        body_snippet = (c.body_text or "")[:50]
        title = f"{sender_name}: {body_snippet}" if body_snippet else sender_name
        items.append({
            "id": c.id,
            "source_type": "chat",
            "title": title,
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
            SELECT cm.id, p.name AS sender_name, cm.summary, cm.body_text AS preview,
                   cm.datetime AS dt,
                   'chat' AS source_type,
                   1 - (cm.embedding <=> CAST(:query_embedding AS vector)) AS similarity
            FROM chat_messages cm
            LEFT JOIN people p ON cm.sender_id = p.id
            WHERE cm.embedding IS NOT NULL
            ORDER BY cm.embedding <=> CAST(:query_embedding AS vector)
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

    url = f"{url_prefix}{item_id}?from=/search" if url_prefix and item_id else None

    # Build title — for chats, use "Sender: body snippet" instead of summary
    source_type = row.get("source_type", "unknown")
    title = row.get("title") or "Untitled"
    if source_type == "chat":
        sender_name = row.get("sender_name") or "Unknown"
        body_snippet = (str(row.get("preview") or ""))[:50]
        title = f"{sender_name}: {body_snippet}" if body_snippet else sender_name

    return {
        "id": item_id,
        "source_type": source_type,
        "title": title,
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
