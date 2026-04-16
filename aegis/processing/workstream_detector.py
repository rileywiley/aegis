"""Workstream detection — 3-layer system for automatic workstream creation and assignment.

Layer 1: Weekly clustering of unassigned items into new workstreams.
Layer 2: Assignment of new items to existing workstreams (every 30 min).
Layer 3: Verification when Layer 1 proposes a new workstream.
Plus lifecycle management (auto-quiet, auto-archive) and re-classification.
"""

import json
import logging
import math
from datetime import date, datetime, timedelta, timezone

from anthropic import AsyncAnthropic
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import (
    ChatMessage,
    Email,
    LLMUsage,
    Meeting,
    Person,
    Workstream,
    WorkstreamItem,
)
from aegis.db.repositories import (
    create_workstream,
    get_workstreams,
    link_item_to_workstream,
    update_workstream,
    upsert_system_health,
)
from aegis.processing.embeddings import embed_batch, embed_text

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"


# ── Cosine Similarity Utility ──────────────────────────────


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors. Pure Python, no numpy."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── Data Structures ────────────────────────────────────────


class UnassignedItem:
    """Lightweight container for an item pending workstream assignment."""

    __slots__ = ("item_type", "item_id", "text", "embedding", "department_id", "participant_ids")

    def __init__(
        self,
        item_type: str,
        item_id: int,
        text: str,
        embedding: list[float] | None = None,
        department_id: int | None = None,
        participant_ids: list[int] | None = None,
    ):
        self.item_type = item_type
        self.item_id = item_id
        self.text = text
        self.embedding = embedding
        self.department_id = department_id
        self.participant_ids = participant_ids or []


# ── Internal Helpers ───────────────────────────────────────


async def _fetch_unassigned_items(
    session: AsyncSession,
    since: datetime,
) -> list[UnassignedItem]:
    """Fetch meetings, emails, and chat_messages from `since` that are NOT in any workstream."""
    items: list[UnassignedItem] = []

    # Sub-query: all (item_type, item_id) already linked
    linked_sub = select(
        WorkstreamItem.item_type, WorkstreamItem.item_id
    ).subquery()

    # Meetings
    stmt = (
        select(Meeting.id, Meeting.title, Meeting.summary, Meeting.embedding)
        .where(Meeting.start_time >= since)
        .where(Meeting.processing_status == "completed")
        .where(
            ~select(linked_sub.c.item_id)
            .where(linked_sub.c.item_type == "meeting")
            .where(linked_sub.c.item_id == Meeting.id)
            .exists()
        )
    )
    result = await session.execute(stmt)
    for row in result.all():
        text = row.summary or row.title or ""
        emb = list(row.embedding) if row.embedding is not None else None
        items.append(UnassignedItem("meeting", row.id, text, emb))

    # Emails (only substantive/contextual)
    stmt = (
        select(Email.id, Email.subject, Email.summary, Email.embedding, Email.sender_id)
        .where(Email.datetime_ >= since)
        .where(Email.processing_status == "completed")
        .where(Email.triage_class.in_(["substantive", "contextual"]))
        .where(
            ~select(linked_sub.c.item_id)
            .where(linked_sub.c.item_type == "email")
            .where(linked_sub.c.item_id == Email.id)
            .exists()
        )
    )
    result = await session.execute(stmt)
    for row in result.all():
        text = row.summary or row.subject or ""
        emb = list(row.embedding) if row.embedding is not None else None
        # Lookup sender's department
        dept_id = None
        if row.sender_id:
            person = await session.get(Person, row.sender_id)
            if person:
                dept_id = person.department_id
        items.append(UnassignedItem("email", row.id, text, emb, dept_id, [row.sender_id] if row.sender_id else []))

    # Chat messages (only substantive/contextual)
    stmt = (
        select(ChatMessage.id, ChatMessage.summary, ChatMessage.body_preview, ChatMessage.embedding, ChatMessage.sender_id)
        .where(ChatMessage.datetime_ >= since)
        .where(ChatMessage.processing_status == "completed")
        .where(ChatMessage.triage_class.in_(["substantive", "contextual"]))
        .where(
            ~select(linked_sub.c.item_id)
            .where(linked_sub.c.item_type == "chat_message")
            .where(linked_sub.c.item_id == ChatMessage.id)
            .exists()
        )
    )
    result = await session.execute(stmt)
    for row in result.all():
        text = row.summary or row.body_preview or ""
        emb = list(row.embedding) if row.embedding is not None else None
        dept_id = None
        if row.sender_id:
            person = await session.get(Person, row.sender_id)
            if person:
                dept_id = person.department_id
        items.append(UnassignedItem("chat_message", row.id, text, emb, dept_id, [row.sender_id] if row.sender_id else []))

    return items


async def _ensure_embeddings(items: list[UnassignedItem]) -> list[UnassignedItem]:
    """Generate embeddings for items that don't have them yet."""
    needs_embedding = [i for i in items if i.embedding is None and i.text]
    if not needs_embedding:
        return items

    texts = [i.text for i in needs_embedding]
    embeddings = await embed_batch(texts)
    for item, emb in zip(needs_embedding, embeddings):
        item.embedding = emb

    return items


def _can_cluster_together(a: UnassignedItem, b: UnassignedItem) -> bool:
    """Org chart partition constraint: items from unrelated departments
    with no shared participants cannot cluster."""
    # If either has no department, allow (we don't have enough info to block)
    if a.department_id is None or b.department_id is None:
        return True
    # Same department: always ok
    if a.department_id == b.department_id:
        return True
    # Different departments: only if they share at least one participant
    shared = set(a.participant_ids) & set(b.participant_ids)
    return len(shared) > 0


def _cluster_items(
    items: list[UnassignedItem],
    similarity_threshold: float = 0.6,
) -> list[list[UnassignedItem]]:
    """Simple greedy clustering by cosine similarity with org chart constraint."""
    if not items:
        return []

    # Filter to items that have valid embeddings
    valid = [i for i in items if i.embedding and any(v != 0.0 for v in i.embedding)]
    if not valid:
        return []

    assigned = set()
    clusters: list[list[UnassignedItem]] = []

    for i, item_a in enumerate(valid):
        if i in assigned:
            continue
        cluster = [item_a]
        assigned.add(i)

        for j in range(i + 1, len(valid)):
            if j in assigned:
                continue
            item_b = valid[j]

            # Check org chart constraint against ALL items in the cluster
            can_join = all(_can_cluster_together(c, item_b) for c in cluster)
            if not can_join:
                continue

            sim = cosine_similarity(item_a.embedding, item_b.embedding)
            if sim >= similarity_threshold:
                cluster.append(item_b)
                assigned.add(j)

        if len(cluster) >= 2:  # At least 2 items to form a potential cluster
            clusters.append(cluster)

    return clusters


def _source_type_count(cluster: list[UnassignedItem]) -> int:
    """Count distinct source types in a cluster."""
    return len({item.item_type for item in cluster})


def _cluster_confidence(cluster: list[UnassignedItem]) -> float:
    """Compute cluster coherence as average pairwise similarity."""
    if len(cluster) < 2:
        return 0.0
    sims = []
    for i in range(len(cluster)):
        for j in range(i + 1, len(cluster)):
            if cluster[i].embedding and cluster[j].embedding:
                sims.append(cosine_similarity(cluster[i].embedding, cluster[j].embedding))
    return sum(sims) / len(sims) if sims else 0.0


async def _name_workstream_via_llm(items_text: list[str]) -> dict[str, str]:
    """Ask Haiku to name and describe a workstream based on its items."""
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    prompt = f"""\
Based on these related items from meetings, emails, and chats, suggest a concise workstream name and a one-sentence description.

Items:
{json.dumps(items_text[:10], indent=2)}

Return JSON: {{"name": "...", "description": "..."}}
"""
    try:
        response = await client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=200,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Extract JSON from response
        if "{" in text:
            json_str = text[text.index("{"):text.rindex("}") + 1]
            result = json.loads(json_str)
            await _track_llm_usage(
                response.usage.input_tokens, response.usage.output_tokens, "workstream_naming"
            )
            return {"name": result.get("name", "Unnamed Workstream"), "description": result.get("description", "")}
    except Exception:
        logger.exception("Failed to name workstream via LLM")

    return {"name": "Unnamed Workstream", "description": ""}


async def _track_llm_usage(input_tokens: int, output_tokens: int, task: str) -> None:
    """Track LLM usage — fire-and-forget style, non-critical."""
    # This needs a session; we'll track usage in the calling functions where session is available
    pass


async def _track_llm_usage_in_session(
    session: AsyncSession, input_tokens: int, output_tokens: int, task: str
) -> None:
    """Record LLM usage in the llm_usage table."""
    today = date.today()
    stmt = pg_insert(LLMUsage).values(
        date=today,
        model=HAIKU_MODEL,
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


# ── Layer 1: Weekly Clustering ─────────────────────────────


async def run_weekly_clustering(session: AsyncSession) -> dict[str, int]:
    """Layer 1 — cluster unassigned items from the last 7 days into new workstreams.

    Returns: {"workstreams_created": N, "items_assigned": M}
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)

    logger.info("Layer 1: starting weekly clustering")

    # Fetch unassigned items
    items = await _fetch_unassigned_items(session, since)
    if not items:
        logger.info("Layer 1: no unassigned items to cluster")
        return {"workstreams_created": 0, "items_assigned": 0}

    # Ensure all items have embeddings
    items = await _ensure_embeddings(items)

    # Cluster
    clusters = _cluster_items(items)

    workstreams_created = 0
    items_assigned = 0

    for cluster in clusters:
        # Spec requirement: 3+ items across 2+ source types
        if len(cluster) < 3 or _source_type_count(cluster) < 2:
            continue

        confidence = _cluster_confidence(cluster)

        if confidence < 0.5:
            continue

        # Name the workstream via LLM
        items_text = [item.text for item in cluster if item.text]
        naming = await _name_workstream_via_llm(items_text)

        # Create a representative embedding (average of cluster embeddings)
        valid_embeddings = [i.embedding for i in cluster if i.embedding]
        if valid_embeddings:
            dim = len(valid_embeddings[0])
            avg_embedding = [
                sum(e[d] for e in valid_embeddings) / len(valid_embeddings)
                for d in range(dim)
            ]
        else:
            avg_embedding = None

        # Determine created_by based on confidence
        auto_threshold = settings.workstream_auto_create_confidence
        ws_confidence = confidence

        ws = await create_workstream(
            session,
            name=naming["name"],
            description=naming["description"],
            created_by="auto",
            confidence=ws_confidence,
            status="active",
        )

        # Set embedding on the workstream
        if avg_embedding:
            await session.execute(
                update(Workstream)
                .where(Workstream.id == ws.id)
                .values(embedding=avg_embedding)
            )
            await session.commit()

        # Verify the new workstream (Layer 3)
        verified = await verify_new_workstream(session, ws.id)
        if not verified:
            # Verification failed (e.g., duplicate detected) — workstream was merged/deleted
            continue

        # Link items
        for item in cluster:
            await link_item_to_workstream(
                session,
                workstream_id=ws.id,
                item_type=item.item_type,
                item_id=item.item_id,
                linked_by="auto",
                relevance_score=confidence,
            )
            items_assigned += 1

        workstreams_created += 1
        logger.info("Layer 1: created workstream '%s' with %d items (confidence=%.2f)",
                     naming["name"], len(cluster), confidence)

    # Update system health
    try:
        await upsert_system_health(
            session,
            service="workstream_detector",
            status="healthy",
            last_success=now,
            items_processed=items_assigned,
        )
    except Exception:
        logger.exception("Failed to update system health for workstream_detector")

    logger.info("Layer 1 complete: %d workstreams created, %d items assigned",
                workstreams_created, items_assigned)
    return {"workstreams_created": workstreams_created, "items_assigned": items_assigned}


# ── Layer 2: Assignment ────────────────────────────────────


async def run_workstream_assignment(session: AsyncSession) -> dict[str, int]:
    """Layer 2 — assign completed items to existing active workstreams.

    Uses embedding similarity with optional Haiku for borderline cases.
    Returns: {"items_assigned": N, "items_unassigned": M}
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)

    logger.info("Layer 2: starting workstream assignment")

    # Fetch unassigned items
    items = await _fetch_unassigned_items(session, since)
    if not items:
        logger.info("Layer 2: no unassigned items")
        return {"items_assigned": 0, "items_unassigned": 0}

    # Ensure embeddings
    items = await _ensure_embeddings(items)

    # Fetch active workstreams with embeddings
    active_workstreams = await get_workstreams(session, status_filter="active")
    ws_with_embeddings = [
        ws for ws in active_workstreams if ws.embedding is not None
    ]

    if not ws_with_embeddings:
        logger.info("Layer 2: no active workstreams with embeddings")
        return {"items_assigned": 0, "items_unassigned": len(items)}

    items_assigned = 0
    items_unassigned = 0
    borderline_batch: list[tuple[UnassignedItem, list[tuple[int, str, float]]]] = []

    for item in items:
        if not item.embedding or all(v == 0.0 for v in item.embedding):
            items_unassigned += 1
            continue

        # Compute similarity against each workstream
        candidates: list[tuple[int, str, float]] = []
        for ws in ws_with_embeddings:
            ws_emb = list(ws.embedding) if ws.embedding is not None else None
            if not ws_emb:
                continue
            sim = cosine_similarity(item.embedding, ws_emb)
            if sim >= 0.4:  # Pre-filter threshold
                candidates.append((ws.id, ws.name, sim))

        if not candidates:
            items_unassigned += 1
            continue

        # Sort by similarity descending
        candidates.sort(key=lambda x: x[2], reverse=True)
        best_ws_id, best_ws_name, best_sim = candidates[0]

        if best_sim >= settings.workstream_assign_high_confidence:
            # High confidence: auto-assign
            await link_item_to_workstream(
                session,
                workstream_id=best_ws_id,
                item_type=item.item_type,
                item_id=item.item_id,
                linked_by="auto",
                relevance_score=best_sim,
            )
            items_assigned += 1
        elif best_sim >= settings.workstream_assign_low_confidence:
            # Medium confidence: assign with lower relevance score
            await link_item_to_workstream(
                session,
                workstream_id=best_ws_id,
                item_type=item.item_type,
                item_id=item.item_id,
                linked_by="auto",
                relevance_score=best_sim,
            )
            items_assigned += 1
        else:
            # Borderline: collect for Haiku batch call
            borderline_batch.append((item, candidates[:3]))

    # Process borderline items with Haiku batch call
    if borderline_batch:
        assigned_from_borderline = await _resolve_borderline_assignments(
            session, borderline_batch
        )
        items_assigned += assigned_from_borderline
        items_unassigned += len(borderline_batch) - assigned_from_borderline

    logger.info("Layer 2 complete: %d assigned, %d unassigned", items_assigned, items_unassigned)
    return {"items_assigned": items_assigned, "items_unassigned": items_unassigned}


async def _resolve_borderline_assignments(
    session: AsyncSession,
    borderline_batch: list[tuple[UnassignedItem, list[tuple[int, str, float]]]],
) -> int:
    """Use Haiku to resolve borderline workstream assignments.

    Returns count of items successfully assigned.
    """
    if not borderline_batch:
        return 0

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Build the batch prompt
    batch_items = []
    for idx, (item, candidates) in enumerate(borderline_batch):
        ws_options = [{"id": ws_id, "name": ws_name, "similarity": round(sim, 3)} for ws_id, ws_name, sim in candidates]
        batch_items.append({
            "index": idx,
            "text": item.text[:500],
            "source_type": item.item_type,
            "workstream_candidates": ws_options,
        })

    prompt = f"""\
For each item below, determine which workstream (if any) it belongs to.
Return a JSON array of objects: [{{"index": 0, "workstream_id": 123, "confidence": 0.75}}, ...]
If an item doesn't fit any workstream, set workstream_id to null.

Items:
{json.dumps(batch_items, indent=2)}
"""
    assigned = 0
    try:
        response = await client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1000,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        await _track_llm_usage_in_session(
            session, response.usage.input_tokens, response.usage.output_tokens, "workstream_assignment"
        )

        # Parse JSON from response
        if "[" in text:
            json_str = text[text.index("["):text.rindex("]") + 1]
            results = json.loads(json_str)

            for result in results:
                idx = result.get("index")
                ws_id = result.get("workstream_id")
                conf = result.get("confidence", 0.0)

                if idx is None or ws_id is None or conf < settings.workstream_assign_low_confidence:
                    continue
                if idx < 0 or idx >= len(borderline_batch):
                    continue

                item, _ = borderline_batch[idx]
                await link_item_to_workstream(
                    session,
                    workstream_id=ws_id,
                    item_type=item.item_type,
                    item_id=item.item_id,
                    linked_by="auto",
                    relevance_score=conf,
                )
                assigned += 1

        await session.commit()
    except Exception:
        logger.exception("Haiku borderline assignment batch failed")

    return assigned


# ── Layer 3: Verification ──────────────────────────────────


async def verify_new_workstream(session: AsyncSession, workstream_id: int) -> bool:
    """Layer 3 — verify a newly proposed workstream is not a duplicate and is coherent.

    Returns True if the workstream is valid and should be kept.
    Returns False if it was merged into an existing workstream or deleted.
    """
    ws = await session.get(Workstream, workstream_id)
    if ws is None:
        return False

    ws_embedding = list(ws.embedding) if ws.embedding is not None else None
    if not ws_embedding:
        return True  # Can't verify without embedding, keep it

    # Dedup check: compare against all other active workstreams
    all_workstreams = await get_workstreams(session, status_filter="active")
    for other in all_workstreams:
        if other.id == workstream_id:
            continue
        other_emb = list(other.embedding) if other.embedding is not None else None
        if not other_emb:
            continue

        sim = cosine_similarity(ws_embedding, other_emb)
        if sim > 0.85:
            # Too similar — merge into the existing workstream
            logger.info(
                "Layer 3: workstream '%s' (id=%d) too similar to '%s' (id=%d, sim=%.3f). Merging.",
                ws.name, ws.id, other.name, other.id, sim,
            )
            # Move all items from new workstream to existing one
            items_stmt = select(WorkstreamItem).where(WorkstreamItem.workstream_id == workstream_id)
            result = await session.execute(items_stmt)
            items = list(result.scalars().all())
            for wi in items:
                await link_item_to_workstream(
                    session,
                    workstream_id=other.id,
                    item_type=wi.item_type,
                    item_id=wi.item_id,
                    linked_by="auto",
                    relevance_score=wi.relevance_score or 1.0,
                )

            # Mark new workstream as merged
            await update_workstream(
                session, workstream_id, status="archived", merged_into_id=other.id
            )
            return False

    # Coherence check via Haiku (only for auto-created workstreams)
    if ws.created_by == "auto":
        items_stmt = (
            select(WorkstreamItem)
            .where(WorkstreamItem.workstream_id == workstream_id)
            .limit(10)
        )
        result = await session.execute(items_stmt)
        ws_items = list(result.scalars().all())

        if len(ws_items) >= 3:
            # Fetch item texts for coherence check
            item_texts = []
            for wi in ws_items:
                text = await _get_item_text(session, wi.item_type, wi.item_id)
                if text:
                    item_texts.append(text)

            if item_texts:
                is_coherent = await _check_coherence_via_llm(session, ws.name, item_texts)
                if not is_coherent:
                    logger.info("Layer 3: workstream '%s' (id=%d) failed coherence check, removing",
                                ws.name, ws.id)
                    # Unlink items and archive
                    await session.execute(
                        update(WorkstreamItem)
                        .where(WorkstreamItem.workstream_id == workstream_id)
                        .values(workstream_id=workstream_id)  # noop, we'll delete below
                    )
                    # Delete workstream items
                    from sqlalchemy import delete
                    await session.execute(
                        delete(WorkstreamItem).where(WorkstreamItem.workstream_id == workstream_id)
                    )
                    await update_workstream(session, workstream_id, status="archived")
                    return False

    return True


async def _get_item_text(session: AsyncSession, item_type: str, item_id: int) -> str | None:
    """Fetch a text summary for any item type."""
    if item_type == "meeting":
        m = await session.get(Meeting, item_id)
        return m.summary or m.title if m else None
    elif item_type == "email":
        e = await session.get(Email, item_id)
        return e.summary or e.subject if e else None
    elif item_type == "chat_message":
        c = await session.get(ChatMessage, item_id)
        return c.summary or c.body_preview if c else None
    return None


async def _check_coherence_via_llm(
    session: AsyncSession, workstream_name: str, item_texts: list[str]
) -> bool:
    """Ask Haiku whether the items in a workstream are genuinely related."""
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    prompt = f"""\
A workstream named "{workstream_name}" was auto-created from these items:
{json.dumps(item_texts[:10], indent=2)}

Are these items genuinely related to a single workstream/initiative?
Return JSON: {{"coherent": true/false, "reason": "..."}}
"""
    try:
        response = await client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=200,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        await _track_llm_usage_in_session(
            session, response.usage.input_tokens, response.usage.output_tokens, "workstream_verification"
        )

        if "{" in text:
            json_str = text[text.index("{"):text.rindex("}") + 1]
            result = json.loads(json_str)
            return result.get("coherent", True)
    except Exception:
        logger.exception("Coherence check LLM call failed")

    # Default to coherent if LLM fails
    return True


# ── Lifecycle Management ───────────────────────────────────


async def manage_workstream_lifecycle(session: AsyncSession) -> dict[str, int]:
    """Auto-quiet and auto-archive workstreams based on inactivity.

    Returns: {"quieted": N, "archived": M}
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    quieted = 0
    archived = 0

    # Fetch active workstreams
    active_workstreams = await get_workstreams(session, status_filter="active")

    for ws in active_workstreams:
        # Find last activity (most recent linked_at in workstream_items)
        stmt = (
            select(func.max(WorkstreamItem.linked_at))
            .where(WorkstreamItem.workstream_id == ws.id)
        )
        result = await session.execute(stmt)
        last_activity = result.scalar_one_or_none()

        if last_activity is None:
            # No items — use workstream creation date
            last_activity = ws.created

        quiet_days = ws.auto_quiet_days or settings.workstream_default_quiet_days
        days_inactive = (now - last_activity).days

        if days_inactive >= quiet_days:
            await update_workstream(session, ws.id, status="quiet")
            quieted += 1
            logger.info("Lifecycle: workstream '%s' (id=%d) marked quiet (%d days inactive)",
                        ws.name, ws.id, days_inactive)

    # Auto-archive: quiet or completed workstreams after 90 days
    quiet_workstreams = await get_workstreams(session, status_filter="quiet")
    completed_workstreams = await get_workstreams(session, status_filter="completed")

    for ws in quiet_workstreams + completed_workstreams:
        days_in_state = (now - ws.updated).days
        if days_in_state >= 90:
            await update_workstream(session, ws.id, status="archived")
            archived += 1
            logger.info("Lifecycle: workstream '%s' (id=%d) archived (%d days in %s)",
                        ws.name, ws.id, days_in_state, ws.status)

    logger.info("Lifecycle complete: %d quieted, %d archived", quieted, archived)
    return {"quieted": quieted, "archived": archived}


# ── Re-classification ──────────────────────────────────────


async def reclassify_after_change(
    session: AsyncSession, workstream_id: int
) -> dict[str, int]:
    """After split/merge/manual creation, re-scan unassigned and adjacent items.

    Runs Layer 2 assignment for affected items.
    Returns: {"items_reassigned": N}
    """
    logger.info("Re-classification: triggered for workstream id=%d", workstream_id)

    # Re-run Layer 2 assignment (which picks up all unassigned items)
    result = await run_workstream_assignment(session)

    logger.info("Re-classification complete: %d items assigned", result["items_assigned"])
    return {"items_reassigned": result["items_assigned"]}
