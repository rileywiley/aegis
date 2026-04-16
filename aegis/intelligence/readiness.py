"""Readiness scoring — workload balance measurement per person.

Computes a 0-100 busyness score per person based on open items, blocking count,
incoming velocity, and workstream count. All components are normalized 0-1
relative to peers.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import (
    ActionItem,
    ChatAsk,
    EmailAsk,
    WorkstreamStakeholder,
)

logger = logging.getLogger("aegis.readiness")


class ReadinessScore(BaseModel):
    person_id: int
    score: int  # 0-100
    open_items: int
    blocking_count: int
    incoming_velocity: float
    workstream_count: int
    trend: Literal["up", "down", "flat"]


async def _count_open_action_items(session: AsyncSession, person_id: int) -> int:
    """Count action items assigned to person with status 'open' or 'in_progress'."""
    stmt = (
        select(func.count())
        .select_from(ActionItem)
        .where(
            ActionItem.assignee_id == person_id,
            ActionItem.status.in_(["open", "in_progress"]),
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one() or 0


async def _count_open_asks(session: AsyncSession, person_id: int) -> int:
    """Count open email_asks + chat_asks targeting this person."""
    email_stmt = (
        select(func.count())
        .select_from(EmailAsk)
        .where(
            EmailAsk.target_id == person_id,
            EmailAsk.status.in_(["open", "in_progress"]),
        )
    )
    chat_stmt = (
        select(func.count())
        .select_from(ChatAsk)
        .where(
            ChatAsk.target_id == person_id,
            ChatAsk.status.in_(["open", "in_progress"]),
        )
    )

    email_result = await session.execute(email_stmt)
    chat_result = await session.execute(chat_stmt)
    return (email_result.scalar_one() or 0) + (chat_result.scalar_one() or 0)


async def _count_blocking(session: AsyncSession, person_id: int) -> int:
    """Count items where others are waiting on this person.

    An item is 'blocking' if this person is the target of an open ask
    or has an open action item assigned to them.
    """
    # For now, blocking == open asks targeting this person
    # (others asked them for something)
    return await _count_open_asks(session, person_id)


async def _count_active_workstreams(session: AsyncSession, person_id: int) -> int:
    """Count active workstreams where this person is a stakeholder."""
    from aegis.db.models import Workstream

    stmt = (
        select(func.count())
        .select_from(WorkstreamStakeholder)
        .join(Workstream, Workstream.id == WorkstreamStakeholder.workstream_id)
        .where(
            WorkstreamStakeholder.person_id == person_id,
            Workstream.status == "active",
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one() or 0


async def _compute_incoming_velocity(session: AsyncSession, person_id: int) -> float:
    """Compute ratio of new items in last 7 days vs completed items.

    Returns a float where >1.0 means items are accumulating.
    Stub: returns 1.0 (flat) until Phase 4 adds full velocity tracking.
    """
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    # Count new action items assigned in last 7 days
    new_stmt = (
        select(func.count())
        .select_from(ActionItem)
        .where(
            ActionItem.assignee_id == person_id,
            ActionItem.created >= week_ago,
        )
    )
    new_result = await session.execute(new_stmt)
    new_count = new_result.scalar_one() or 0

    # Count completed action items in last 7 days
    completed_stmt = (
        select(func.count())
        .select_from(ActionItem)
        .where(
            ActionItem.assignee_id == person_id,
            ActionItem.status == "completed",
            ActionItem.updated >= week_ago,
        )
    )
    completed_result = await session.execute(completed_stmt)
    completed_count = completed_result.scalar_one() or 0

    if completed_count == 0:
        return float(new_count) if new_count > 0 else 0.0
    return new_count / completed_count


async def compute_readiness(session: AsyncSession, person_id: int) -> ReadinessScore:
    """Compute readiness score for a single person.

    Formula: (open_items * 0.30 + blocking * 0.25 + velocity * 0.25
              + workstreams * 0.20) * 100

    All components are normalized 0-1 using reasonable maximums.
    Scores are relative -- 0 means idle, 100 means heavily loaded.
    """
    open_action_items = await _count_open_action_items(session, person_id)
    open_asks = await _count_open_asks(session, person_id)
    open_items = open_action_items + open_asks

    blocking_count = await _count_blocking(session, person_id)
    workstream_count = await _count_active_workstreams(session, person_id)
    velocity = await _compute_incoming_velocity(session, person_id)

    # Normalize components to 0-1 range with sensible caps
    norm_items = min(open_items / 20.0, 1.0)         # 20+ items = maxed
    norm_blocking = min(blocking_count / 10.0, 1.0)   # 10+ blocking = maxed
    norm_velocity = min(velocity / 3.0, 1.0)           # 3x incoming vs completed = maxed
    norm_workstreams = min(workstream_count / 8.0, 1.0)  # 8+ workstreams = maxed

    raw_score = (
        norm_items * 0.30
        + norm_blocking * 0.25
        + norm_velocity * 0.25
        + norm_workstreams * 0.20
    ) * 100

    score = max(0, min(100, int(round(raw_score))))

    # Trend: compare current velocity to baseline
    # Rising velocity (>1.5) means workload increasing, falling (<0.7) decreasing
    if velocity > 1.5:
        trend: Literal["up", "down", "flat"] = "up"
    elif velocity < 0.7 and open_items < 5:
        trend = "down"
    else:
        trend = "flat"

    return ReadinessScore(
        person_id=person_id,
        score=score,
        open_items=open_items,
        blocking_count=blocking_count,
        incoming_velocity=round(velocity, 2),
        workstream_count=workstream_count,
        trend=trend,
    )


async def compute_all_readiness(
    session: AsyncSession,
    person_ids: list[int] | None = None,
) -> list[ReadinessScore]:
    """Compute readiness scores for multiple people.

    If person_ids is None, computes for all non-external people
    with interaction_count > 0.
    """
    from aegis.db.models import Person

    if person_ids is None:
        stmt = select(Person.id).where(
            Person.is_external.is_(False),
            Person.interaction_count > 0,
        )
        result = await session.execute(stmt)
        person_ids = list(result.scalars().all())

    scores = []
    for pid in person_ids:
        score = await compute_readiness(session, pid)
        scores.append(score)

    return scores


async def get_readiness_detail(
    session: AsyncSession, person_id: int
) -> dict:
    """Get detailed breakdown of open items for a person.

    Returns dict with lists of action_items, email_asks, and chat_asks.
    """
    # Open action items
    ai_stmt = (
        select(ActionItem)
        .where(
            ActionItem.assignee_id == person_id,
            ActionItem.status.in_(["open", "in_progress"]),
        )
        .order_by(ActionItem.created.desc())
        .limit(20)
    )
    ai_result = await session.execute(ai_stmt)
    action_items = list(ai_result.scalars().all())

    # Open email asks targeting this person
    ea_stmt = (
        select(EmailAsk)
        .where(
            EmailAsk.target_id == person_id,
            EmailAsk.status.in_(["open", "in_progress"]),
        )
        .order_by(EmailAsk.created.desc())
        .limit(20)
    )
    ea_result = await session.execute(ea_stmt)
    email_asks = list(ea_result.scalars().all())

    # Open chat asks targeting this person
    ca_stmt = (
        select(ChatAsk)
        .where(
            ChatAsk.target_id == person_id,
            ChatAsk.status.in_(["open", "in_progress"]),
        )
        .order_by(ChatAsk.created.desc())
        .limit(20)
    )
    ca_result = await session.execute(ca_stmt)
    chat_asks = list(ca_result.scalars().all())

    return {
        "action_items": action_items,
        "email_asks": email_asks,
        "chat_asks": chat_asks,
    }
