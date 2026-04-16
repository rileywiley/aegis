"""Org inference — bootstrap org structure from calendar patterns and title heuristics."""

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import (
    Department,
    Meeting,
    MeetingAttendee,
    Person,
    PersonHistory,
)

logger = logging.getLogger("aegis.org_inference")

# ── Title-based seniority mapping ────────────────────────

_EXECUTIVE_PATTERNS = re.compile(
    r"\b(c[efiost]o|chief|president|founder|partner|managing\s+director)\b",
    re.IGNORECASE,
)
_SENIOR_PATTERNS = re.compile(
    r"\b(vp|vice\s+president|director|head\s+of|svp|evp|principal|fellow)\b",
    re.IGNORECASE,
)
_MID_PATTERNS = re.compile(
    r"\b(manager|lead|senior|sr\.?|staff|supervisor|coordinator)\b",
    re.IGNORECASE,
)
_JUNIOR_PATTERNS = re.compile(
    r"\b(analyst|associate|specialist|intern|trainee|assistant|junior|jr\.?)\b",
    re.IGNORECASE,
)


def infer_seniority_from_title(title: str | None) -> str:
    """Return seniority level based on title keywords. Returns 'unknown' if no match."""
    if not title:
        return "unknown"
    if _EXECUTIVE_PATTERNS.search(title):
        return "executive"
    if _SENIOR_PATTERNS.search(title):
        return "senior"
    if _MID_PATTERNS.search(title):
        return "mid"
    if _JUNIOR_PATTERNS.search(title):
        return "junior"
    return "unknown"


async def _log_change(
    session: AsyncSession,
    person_id: int,
    field: str,
    old_value: str | None,
    new_value: str | None,
) -> None:
    """Record a change in the people_history table."""
    if str(old_value) == str(new_value):
        return
    entry = PersonHistory(
        person_id=person_id,
        field_changed=field,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None,
        change_source="inferred",
    )
    session.add(entry)


async def _infer_seniority(session: AsyncSession) -> int:
    """Update seniority for people whose title provides a clear signal.

    Only updates people with seniority='unknown' who have a title set.
    Returns count of updates.
    """
    stmt = select(Person).where(
        Person.title.isnot(None),
        Person.title != "",
        Person.seniority == "unknown",
    )
    result = await session.execute(stmt)
    people = list(result.scalars().all())

    updated = 0
    for person in people:
        new_seniority = infer_seniority_from_title(person.title)
        if new_seniority != "unknown":
            await _log_change(session, person.id, "seniority", person.seniority, new_seniority)
            person.seniority = new_seniority
            updated += 1

    if updated:
        await session.flush()
    logger.info("Seniority inference: updated %d people", updated)
    return updated


async def _detect_one_on_ones(session: AsyncSession) -> dict[int, list[int]]:
    """Find recurring 2-person meetings and return {person_id: [partner_ids]}.

    A recurring 1:1 is defined as: same recurring_series_id, exactly 2 attendees,
    at least 3 occurrences.
    """
    # Find series with exactly 2 attendees that recur 3+ times
    # Step 1: get meetings that are part of a recurring series
    series_meetings = (
        select(
            Meeting.recurring_series_id,
            Meeting.id.label("meeting_id"),
        )
        .where(
            Meeting.recurring_series_id.isnot(None),
            Meeting.is_excluded.is_(False),
        )
        .subquery()
    )

    # Step 2: for each meeting, count attendees
    attendee_count = (
        select(
            MeetingAttendee.meeting_id,
            func.count(MeetingAttendee.person_id).label("cnt"),
        )
        .group_by(MeetingAttendee.meeting_id)
        .having(func.count(MeetingAttendee.person_id) == 2)
        .subquery()
    )

    # Step 3: join series meetings with 2-attendee meetings, count per series
    stmt = (
        select(
            series_meetings.c.recurring_series_id,
            func.count(series_meetings.c.meeting_id).label("occurrence_count"),
        )
        .join(
            attendee_count,
            attendee_count.c.meeting_id == series_meetings.c.meeting_id,
        )
        .group_by(series_meetings.c.recurring_series_id)
        .having(func.count(series_meetings.c.meeting_id) >= 3)
    )

    result = await session.execute(stmt)
    qualifying_series = [row.recurring_series_id for row in result.all()]

    if not qualifying_series:
        return {}

    # For qualifying series, get the attendee pairs
    one_on_ones: dict[int, list[int]] = defaultdict(list)

    for series_id in qualifying_series:
        # Get attendees from any meeting in the series
        meeting_stmt = (
            select(Meeting.id)
            .where(Meeting.recurring_series_id == series_id)
            .limit(1)
        )
        meeting_result = await session.execute(meeting_stmt)
        sample_meeting_id = meeting_result.scalar_one_or_none()
        if not sample_meeting_id:
            continue

        attendee_stmt = select(MeetingAttendee.person_id).where(
            MeetingAttendee.meeting_id == sample_meeting_id
        )
        attendee_result = await session.execute(attendee_stmt)
        attendee_ids = [row for row in attendee_result.scalars().all()]

        if len(attendee_ids) == 2:
            a, b = attendee_ids
            one_on_ones[a].append(b)
            one_on_ones[b].append(a)

    return dict(one_on_ones)


async def _infer_managers(session: AsyncSession) -> int:
    """Infer manager relationships from 1:1 meeting patterns.

    Heuristic: If person A has recurring 1:1s with person B, and A has more
    1:1 partners overall (higher seniority signal), A may be B's manager.
    Also considers the seniority field: higher seniority wins.

    Only sets manager_id if it's currently NULL.
    Returns count of manager relationships set.
    """
    one_on_ones = await _detect_one_on_ones(session)
    if not one_on_ones:
        logger.info("Manager inference: no recurring 1:1 patterns found")
        return 0

    # Load people involved
    person_ids = set(one_on_ones.keys())
    for partners in one_on_ones.values():
        person_ids.update(partners)

    stmt = select(Person).where(Person.id.in_(person_ids))
    result = await session.execute(stmt)
    people_map: dict[int, Person] = {p.id: p for p in result.scalars().all()}

    seniority_rank = {"executive": 4, "senior": 3, "mid": 2, "junior": 1, "unknown": 0}

    updated = 0
    for person_id, partners in one_on_ones.items():
        person = people_map.get(person_id)
        if not person or person.manager_id is not None:
            continue  # already has a manager

        # Find the most likely manager among 1:1 partners
        best_candidate: int | None = None
        best_score = -1

        for partner_id in partners:
            partner = people_map.get(partner_id)
            if not partner:
                continue

            # Score: seniority rank + number of 1:1 partners (breadth of reports)
            partner_seniority = seniority_rank.get(partner.seniority or "unknown", 0)
            person_seniority = seniority_rank.get(person.seniority or "unknown", 0)

            # Partner must be same level or higher to be a manager candidate
            if partner_seniority < person_seniority:
                continue

            partner_breadth = len(one_on_ones.get(partner_id, []))
            person_breadth = len(partners)

            # Partner with more 1:1s and higher/equal seniority is likely the manager
            if partner_breadth > person_breadth or partner_seniority > person_seniority:
                score = partner_seniority * 10 + partner_breadth
                if score > best_score:
                    best_score = score
                    best_candidate = partner_id

        if best_candidate is not None:
            await _log_change(session, person_id, "manager_id", None, str(best_candidate))
            person.manager_id = best_candidate
            updated += 1

    if updated:
        await session.flush()
    logger.info("Manager inference: set %d manager relationships", updated)
    return updated


async def _cluster_departments(session: AsyncSession) -> int:
    """Group people who frequently co-attend meetings into departments.

    Heuristic: For each pair of people who share 3+ meetings, they are likely
    in the same department. Build clusters using connected components, then
    create or reuse Department records.

    Only assigns department_id to people who don't have one yet.
    Returns count of departments created.
    """
    # Get all non-excluded meetings with their attendees
    stmt = (
        select(
            MeetingAttendee.meeting_id,
            MeetingAttendee.person_id,
        )
        .join(Meeting, Meeting.id == MeetingAttendee.meeting_id)
        .where(Meeting.is_excluded.is_(False))
    )
    result = await session.execute(stmt)
    rows = result.all()

    # Build meeting -> attendees mapping
    meeting_people: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        meeting_people[row.meeting_id].append(row.person_id)

    # Count co-attendance pairs (only consider meetings with 2-8 people,
    # larger meetings are too broad to signal department affiliation)
    pair_counts: Counter = Counter()
    for _meeting_id, attendees in meeting_people.items():
        if 2 <= len(attendees) <= 8:
            for i, a in enumerate(attendees):
                for b in attendees[i + 1 :]:
                    pair = (min(a, b), max(a, b))
                    pair_counts[pair] += 1

    # Filter to pairs with 3+ shared meetings
    strong_pairs = {pair for pair, count in pair_counts.items() if count >= 3}
    if not strong_pairs:
        logger.info("Department clustering: no strong co-attendance pairs found")
        return 0

    # Build adjacency graph and find connected components
    adj: dict[int, set[int]] = defaultdict(set)
    for a, b in strong_pairs:
        adj[a].add(b)
        adj[b].add(a)

    visited: set[int] = set()
    clusters: list[set[int]] = []

    for node in adj:
        if node in visited:
            continue
        # BFS
        cluster: set[int] = set()
        queue = [node]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            cluster.add(current)
            for neighbor in adj[current]:
                if neighbor not in visited:
                    queue.append(neighbor)
        if len(cluster) >= 2:
            clusters.append(cluster)

    if not clusters:
        logger.info("Department clustering: no clusters with 2+ members")
        return 0

    # Only assign to people who don't have a department yet
    unassigned_stmt = select(Person).where(Person.department_id.is_(None))
    result = await session.execute(unassigned_stmt)
    unassigned_people = {p.id: p for p in result.scalars().all()}

    # Load existing departments to avoid duplicates
    dept_stmt = select(Department)
    result = await session.execute(dept_stmt)
    existing_depts = list(result.scalars().all())

    departments_created = 0

    for i, cluster in enumerate(clusters):
        # Only proceed if cluster has unassigned members
        unassigned_in_cluster = [pid for pid in cluster if pid in unassigned_people]
        if not unassigned_in_cluster:
            continue

        # Check if most cluster members already belong to an existing department
        assigned_depts: Counter = Counter()
        for pid in cluster:
            if pid not in unassigned_people:
                # Look up this person's department
                p_stmt = select(Person.department_id).where(Person.id == pid)
                p_result = await session.execute(p_stmt)
                dept_id = p_result.scalar_one_or_none()
                if dept_id:
                    assigned_depts[dept_id] += 1

        if assigned_depts:
            # Use the most common existing department
            target_dept_id = assigned_depts.most_common(1)[0][0]
        else:
            # Create a new department
            dept = Department(
                name=f"Department {len(existing_depts) + departments_created + 1}",
                description=f"Auto-inferred from meeting co-attendance ({len(cluster)} members)",
                source="inferred",
                confidence=0.5,
            )
            session.add(dept)
            await session.flush()
            target_dept_id = dept.id
            departments_created += 1

        # Assign unassigned members to this department
        for pid in unassigned_in_cluster:
            person = unassigned_people[pid]
            await _log_change(session, pid, "department_id", None, str(target_dept_id))
            person.department_id = target_dept_id

    if departments_created or unassigned_in_cluster:
        await session.flush()
    logger.info("Department clustering: created %d departments", departments_created)
    return departments_created


async def infer_org_structure(session: AsyncSession) -> dict:
    """Run all org inference heuristics and return a stats summary.

    This is the main entry point. Call it periodically (e.g., weekly batch)
    or during Phase 0 backfill to bootstrap the org chart.

    Returns:
        dict with keys: managers_inferred, seniority_updated, departments_created
    """
    logger.info("Starting org structure inference")

    seniority_updated = await _infer_seniority(session)
    managers_inferred = await _infer_managers(session)
    departments_created = await _cluster_departments(session)

    await session.commit()

    stats = {
        "managers_inferred": managers_inferred,
        "seniority_updated": seniority_updated,
        "departments_created": departments_created,
    }
    logger.info("Org inference complete: %s", stats)
    return stats
