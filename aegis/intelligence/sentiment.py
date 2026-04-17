"""Sentiment aggregation and friction detection.

Computes rolling sentiment scores per person, department, workstream,
and cross-department relationships. Stores results in sentiment_aggregations table.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import (
    ChatMessage,
    Department,
    Email,
    Meeting,
    MeetingAttendee,
    Person,
    SentimentAggregation,
    Workstream,
    WorkstreamItem,
)

logger = logging.getLogger("aegis.sentiment")

# Map sentiment string labels to numeric scores (0-100 scale)
SENTIMENT_SCORES: dict[str, int] = {
    "positive": 90,
    "neutral": 60,
    "tense": 40,
    "negative": 20,
    "urgent": 30,
}


def _sentiment_to_score(sentiment: str | None) -> int | None:
    """Convert a sentiment label to a numeric score, or None if not set."""
    if sentiment is None:
        return None
    return SENTIMENT_SCORES.get(sentiment)


def _compute_trend(
    recent_scores: list[int], earlier_scores: list[int]
) -> Literal["up", "down", "flat"]:
    """Compare two sets of scores to determine trend direction.

    'recent' is the most recent window, 'earlier' is the preceding window.
    """
    if not recent_scores or not earlier_scores:
        return "flat"
    recent_avg = sum(recent_scores) / len(recent_scores)
    earlier_avg = sum(earlier_scores) / len(earlier_scores)
    diff = recent_avg - earlier_avg
    if diff > 5:
        return "up"
    elif diff < -5:
        return "down"
    return "flat"


async def _get_person_meeting_sentiments(
    session: AsyncSession, person_id: int, since: datetime, until: datetime
) -> list[int]:
    """Get sentiment scores from meetings this person attended in a date range."""
    stmt = (
        select(Meeting.sentiment)
        .join(MeetingAttendee, MeetingAttendee.meeting_id == Meeting.id)
        .where(
            MeetingAttendee.person_id == person_id,
            Meeting.start_time >= since,
            Meeting.start_time < until,
            Meeting.sentiment.isnot(None),
        )
    )
    result = await session.execute(stmt)
    scores = []
    for (sentiment,) in result.all():
        score = _sentiment_to_score(sentiment)
        if score is not None:
            scores.append(score)
    return scores


async def _get_person_email_sentiments(
    session: AsyncSession, person_id: int, since: datetime, until: datetime
) -> list[int]:
    """Get sentiment scores from emails involving this person."""
    stmt = (
        select(Email.sentiment)
        .where(
            Email.sender_id == person_id,
            Email.datetime_ >= since,
            Email.datetime_ < until,
            Email.sentiment.isnot(None),
        )
    )
    result = await session.execute(stmt)
    scores = []
    for (sentiment,) in result.all():
        score = _sentiment_to_score(sentiment)
        if score is not None:
            scores.append(score)
    return scores


async def _get_person_chat_sentiments(
    session: AsyncSession, person_id: int, since: datetime, until: datetime
) -> list[int]:
    """Get sentiment scores from chat messages sent by this person."""
    stmt = (
        select(ChatMessage.sentiment)
        .where(
            ChatMessage.sender_id == person_id,
            ChatMessage.datetime_ >= since,
            ChatMessage.datetime_ < until,
            ChatMessage.sentiment.isnot(None),
        )
    )
    result = await session.execute(stmt)
    scores = []
    for (sentiment,) in result.all():
        score = _sentiment_to_score(sentiment)
        if score is not None:
            scores.append(score)
    return scores


async def _upsert_aggregation(
    session: AsyncSession,
    scope_type: str,
    scope_id: str,
    period_start: date,
    period_end: date,
    avg_score: float,
    interaction_count: int,
    trend: str,
) -> None:
    """Upsert a sentiment aggregation row."""
    # Check for existing row
    stmt = select(SentimentAggregation).where(
        SentimentAggregation.scope_type == scope_type,
        SentimentAggregation.scope_id == scope_id,
        SentimentAggregation.period_start == period_start,
    )
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        existing.avg_score = avg_score
        existing.interaction_count = interaction_count
        existing.trend = trend
        existing.period_end = period_end
        existing.computed_at = datetime.now(timezone.utc)
    else:
        agg = SentimentAggregation(
            scope_type=scope_type,
            scope_id=scope_id,
            period_start=period_start,
            period_end=period_end,
            avg_score=avg_score,
            interaction_count=interaction_count,
            trend=trend,
            computed_at=datetime.now(timezone.utc),
        )
        session.add(agg)


async def compute_sentiment_aggregations(session: AsyncSession) -> dict:
    """Compute sentiment aggregations for all scopes.

    Returns a stats dict with counts of aggregations computed per scope type.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    window_days = settings.sentiment_rolling_window_days
    trend_days = settings.sentiment_trend_window_days

    period_end = now.date()
    period_start = period_end - timedelta(days=window_days)

    window_start = now - timedelta(days=window_days)
    trend_split = now - timedelta(days=trend_days)
    trend_earlier_start = trend_split - timedelta(days=trend_days)

    stats = {"person": 0, "department": 0, "workstream": 0}

    # ── Per-person sentiment ──────────────────────────────
    person_stmt = select(Person.id).where(
        Person.is_external.is_(False),
        Person.interaction_count > 0,
    )
    person_result = await session.execute(person_stmt)
    person_ids = list(person_result.scalars().all())

    person_scores_map: dict[int, float] = {}

    for pid in person_ids:
        # Full window scores
        meeting_scores = await _get_person_meeting_sentiments(
            session, pid, window_start, now
        )
        email_scores = await _get_person_email_sentiments(
            session, pid, window_start, now
        )
        chat_scores = await _get_person_chat_sentiments(
            session, pid, window_start, now
        )
        all_scores = meeting_scores + email_scores + chat_scores

        if not all_scores:
            continue

        avg_score = sum(all_scores) / len(all_scores)
        person_scores_map[pid] = avg_score

        # Compute trend: recent window vs earlier window
        recent_meeting = await _get_person_meeting_sentiments(
            session, pid, trend_split, now
        )
        recent_email = await _get_person_email_sentiments(
            session, pid, trend_split, now
        )
        recent_chat = await _get_person_chat_sentiments(
            session, pid, trend_split, now
        )
        recent_all = recent_meeting + recent_email + recent_chat

        earlier_meeting = await _get_person_meeting_sentiments(
            session, pid, trend_earlier_start, trend_split
        )
        earlier_email = await _get_person_email_sentiments(
            session, pid, trend_earlier_start, trend_split
        )
        earlier_chat = await _get_person_chat_sentiments(
            session, pid, trend_earlier_start, trend_split
        )
        earlier_all = earlier_meeting + earlier_email + earlier_chat

        trend = _compute_trend(recent_all, earlier_all)

        await _upsert_aggregation(
            session,
            scope_type="person",
            scope_id=str(pid),
            period_start=period_start,
            period_end=period_end,
            avg_score=round(avg_score, 1),
            interaction_count=len(all_scores),
            trend=trend,
        )
        stats["person"] += 1

    # ── Per-department sentiment ──────────────────────────
    dept_stmt = select(Department.id)
    dept_result = await session.execute(dept_stmt)
    dept_ids = list(dept_result.scalars().all())

    for dept_id in dept_ids:
        # Get member IDs for this department
        member_stmt = select(Person.id).where(Person.department_id == dept_id)
        member_result = await session.execute(member_stmt)
        member_ids = list(member_result.scalars().all())

        if not member_ids:
            continue

        # Average of member sentiment scores
        dept_scores = [
            person_scores_map[mid]
            for mid in member_ids
            if mid in person_scores_map
        ]
        if not dept_scores:
            continue

        dept_avg = sum(dept_scores) / len(dept_scores)

        # Trend: simple average of member trends not practical,
        # so recompute from aggregated recent vs earlier
        dept_recent: list[int] = []
        dept_earlier: list[int] = []
        for mid in member_ids:
            dept_recent.extend(
                await _get_person_meeting_sentiments(session, mid, trend_split, now)
            )
            dept_recent.extend(
                await _get_person_email_sentiments(session, mid, trend_split, now)
            )
            dept_earlier.extend(
                await _get_person_meeting_sentiments(
                    session, mid, trend_earlier_start, trend_split
                )
            )
            dept_earlier.extend(
                await _get_person_email_sentiments(
                    session, mid, trend_earlier_start, trend_split
                )
            )

        trend = _compute_trend(dept_recent, dept_earlier)

        await _upsert_aggregation(
            session,
            scope_type="department",
            scope_id=str(dept_id),
            period_start=period_start,
            period_end=period_end,
            avg_score=round(dept_avg, 1),
            interaction_count=len(dept_scores),
            trend=trend,
        )
        stats["department"] += 1

    # ── Per-workstream sentiment ──────────────────────────
    ws_stmt = select(Workstream.id).where(Workstream.status == "active")
    ws_result = await session.execute(ws_stmt)
    ws_ids = list(ws_result.scalars().all())

    for ws_id in ws_ids:
        # Get sentiment from linked meetings
        meeting_sent_stmt = (
            select(Meeting.sentiment)
            .join(
                WorkstreamItem,
                and_(
                    WorkstreamItem.item_type == "meeting",
                    WorkstreamItem.item_id == Meeting.id,
                ),
            )
            .where(
                WorkstreamItem.workstream_id == ws_id,
                Meeting.start_time >= window_start,
                Meeting.sentiment.isnot(None),
            )
        )
        meeting_result = await session.execute(meeting_sent_stmt)
        ws_scores = [
            _sentiment_to_score(s)
            for (s,) in meeting_result.all()
            if _sentiment_to_score(s) is not None
        ]

        # Get sentiment from linked emails
        email_sent_stmt = (
            select(Email.sentiment)
            .join(
                WorkstreamItem,
                and_(
                    WorkstreamItem.item_type == "email",
                    WorkstreamItem.item_id == Email.id,
                ),
            )
            .where(
                WorkstreamItem.workstream_id == ws_id,
                Email.datetime_ >= window_start,
                Email.sentiment.isnot(None),
            )
        )
        email_result = await session.execute(email_sent_stmt)
        ws_scores.extend([
            _sentiment_to_score(s)
            for (s,) in email_result.all()
            if _sentiment_to_score(s) is not None
        ])

        if not ws_scores:
            continue

        ws_avg = sum(ws_scores) / len(ws_scores)

        await _upsert_aggregation(
            session,
            scope_type="workstream",
            scope_id=str(ws_id),
            period_start=period_start,
            period_end=period_end,
            avg_score=round(ws_avg, 1),
            interaction_count=len(ws_scores),
            trend="flat",  # Workstream trend not critical; simplified
        )
        stats["workstream"] += 1

    await session.commit()
    logger.info(
        "Sentiment aggregation complete: %d person, %d department, %d workstream",
        stats["person"],
        stats["department"],
        stats["workstream"],
    )
    return stats


async def detect_friction(session: AsyncSession) -> list[dict]:
    """Find cross-department pairs with avg sentiment below friction threshold.

    Returns list of dicts with: person1, person2, dept1, dept2, avg_score, trend.
    """
    settings = get_settings()
    threshold = settings.sentiment_friction_threshold
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=settings.sentiment_rolling_window_days)

    # Find all cross-department meeting pairs (people from different departments
    # who attended the same meetings)
    # We look at meetings with tense/negative/urgent sentiment
    stmt = (
        select(
            MeetingAttendee.person_id.label("p1"),
            func.array_agg(Meeting.sentiment).label("sentiments"),
        )
        .join(Meeting, Meeting.id == MeetingAttendee.meeting_id)
        .where(
            Meeting.start_time >= window_start,
            Meeting.sentiment.in_(["tense", "negative", "urgent"]),
        )
        .group_by(MeetingAttendee.person_id)
    )
    # Instead: find pairs of attendees in the same tense/negative meetings
    # Group meetings with low sentiment, find co-attendees from different depts

    # Get all meetings with low sentiment in the window
    low_meetings_stmt = (
        select(Meeting.id)
        .where(
            Meeting.start_time >= window_start,
            Meeting.sentiment.in_(["tense", "negative", "urgent"]),
        )
    )
    low_result = await session.execute(low_meetings_stmt)
    low_meeting_ids = list(low_result.scalars().all())

    if not low_meeting_ids:
        return []

    # For each low-sentiment meeting, get attendees with their departments
    friction_pairs: dict[tuple[int, int], list[int]] = {}

    for meeting_id in low_meeting_ids:
        attendee_stmt = (
            select(Person.id, Person.department_id)
            .join(MeetingAttendee, MeetingAttendee.person_id == Person.id)
            .where(
                MeetingAttendee.meeting_id == meeting_id,
                Person.department_id.isnot(None),
            )
        )
        att_result = await session.execute(attendee_stmt)
        attendees = list(att_result.all())

        # Find cross-department pairs
        for i, (p1_id, d1_id) in enumerate(attendees):
            for p2_id, d2_id in attendees[i + 1 :]:
                if d1_id != d2_id:
                    pair_key = (min(d1_id, d2_id), max(d1_id, d2_id))
                    meeting_score = _sentiment_to_score(None)
                    # Get actual meeting sentiment
                    m = await session.get(Meeting, meeting_id)
                    if m and m.sentiment:
                        score = _sentiment_to_score(m.sentiment)
                        if score is not None:
                            friction_pairs.setdefault(pair_key, []).append(score)

    # Filter pairs below threshold
    results = []
    for (d1_id, d2_id), scores in friction_pairs.items():
        if not scores:
            continue
        avg_score = sum(scores) / len(scores)
        if avg_score < threshold:
            # Get department names
            d1 = await session.get(Department, d1_id)
            d2 = await session.get(Department, d2_id)
            if d1 and d2:
                results.append({
                    "dept1_id": d1_id,
                    "dept2_id": d2_id,
                    "dept1": d1.name,
                    "dept2": d2.name,
                    "avg_score": round(avg_score, 1),
                    "interaction_count": len(scores),
                    "trend": "flat",
                })

    results.sort(key=lambda x: x["avg_score"])
    return results


async def get_department_sentiment(
    session: AsyncSession, dept_id: int
) -> dict | None:
    """Get the latest sentiment aggregation for a department.

    Returns dict with avg_score, interaction_count, trend, or None if no data.
    """
    stmt = (
        select(SentimentAggregation)
        .where(
            SentimentAggregation.scope_type == "department",
            SentimentAggregation.scope_id == str(dept_id),
        )
        .order_by(SentimentAggregation.computed_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    agg = result.scalar_one_or_none()
    if not agg:
        return None
    return {
        "avg_score": agg.avg_score,
        "interaction_count": agg.interaction_count,
        "trend": agg.trend,
    }
