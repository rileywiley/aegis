"""Org inference — bootstrap org structure from calendar patterns and title heuristics."""

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import (
    Department,
    Email,
    Meeting,
    MeetingAttendee,
    Person,
    PersonHistory,
    Team,
    TeamMembership,
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


async def compute_cc_gravity(session: AsyncSession) -> dict:
    """Analyze email CC patterns to identify influence/importance.

    For each person, count how often they appear in CC of emails sent by others.
    Higher CC gravity = more organizational influence. Updates cc_gravity_score
    on Person records.

    Returns:
        dict with key: people_updated
    """
    # Fetch all emails with recipients (CC list is in the recipients JSONB)
    stmt = select(Email.sender_id, Email.recipients).where(
        Email.recipients.isnot(None),
        Email.sender_id.isnot(None),
    )
    result = await session.execute(stmt)
    rows = result.all()

    # Count CC appearances per person email
    cc_counts: Counter = Counter()
    for sender_id, recipients in rows:
        if not isinstance(recipients, list):
            continue
        for recip in recipients:
            if isinstance(recip, dict) and recip.get("type") == "cc":
                email_addr = recip.get("email", "").lower().strip()
                if email_addr:
                    cc_counts[email_addr] += 1

    if not cc_counts:
        logger.info("CC gravity: no CC recipients found in emails")
        return {"people_updated": 0}

    # Normalize scores (0.0 to 1.0 relative to max)
    max_cc = max(cc_counts.values()) if cc_counts else 1

    # Load people by email for matching
    emails_to_check = list(cc_counts.keys())
    stmt = select(Person).where(
        func.lower(Person.email).in_(emails_to_check)
    )
    result = await session.execute(stmt)
    people = list(result.scalars().all())

    updated = 0
    for person in people:
        if not person.email:
            continue
        count = cc_counts.get(person.email.lower().strip(), 0)
        if count > 0:
            new_score = round(count / max_cc, 3)
            if abs(person.cc_gravity_score - new_score) > 0.001:
                person.cc_gravity_score = new_score
                updated += 1

    if updated:
        await session.flush()
    logger.info("CC gravity: updated %d people", updated)
    return {"people_updated": updated}


# ── Email signature parsing patterns ─────────────────────

_SIG_SEPARATOR = re.compile(
    r"(?:^|\n)(?:--|__+|~~+|Best\s+regards|Kind\s+regards|Thanks|Regards|Cheers|Sincerely)",
    re.IGNORECASE,
)

# Pattern: "Name | Title | Department" or "Name - Title - Department"
_SIG_PIPE_PATTERN = re.compile(
    r"^([^|\-]+)\s*[|\-]\s*([^|\-]+?)(?:\s*[|\-]\s*(.+))?$"
)

# Pattern: title-like line (often follows name)
_TITLE_KEYWORDS = re.compile(
    r"\b(director|manager|engineer|analyst|designer|consultant|coordinator|"
    r"specialist|architect|developer|lead|head|chief|vp|vice\s+president|"
    r"president|officer|associate|senior|principal|staff)\b",
    re.IGNORECASE,
)

# Department keywords
_DEPT_KEYWORDS = re.compile(
    r"\b(engineering|marketing|sales|finance|hr|human\s+resources|operations|"
    r"product|design|legal|compliance|security|it|infrastructure|data|"
    r"research|communications|support|customer\s+success|business\s+development)\b",
    re.IGNORECASE,
)


def _extract_title_dept_from_signature(body_text: str) -> tuple[str | None, str | None]:
    """Parse email signature to extract title and department using regex.

    Returns (title, department) — either may be None.
    """
    if not body_text:
        return None, None

    # Find signature block
    sig_match = _SIG_SEPARATOR.search(body_text)
    if sig_match:
        sig_text = body_text[sig_match.start():]
    else:
        # Use last 500 chars as fallback
        sig_text = body_text[-500:]

    lines = [line.strip() for line in sig_text.split("\n") if line.strip()]

    title: str | None = None
    department: str | None = None

    for line in lines:
        # Skip very long lines (unlikely to be signature elements)
        if len(line) > 100:
            continue

        # Try pipe/dash separated pattern
        pipe_match = _SIG_PIPE_PATTERN.match(line)
        if pipe_match:
            part2 = pipe_match.group(2).strip()
            part3 = pipe_match.group(3)
            if _TITLE_KEYWORDS.search(part2):
                title = part2
            if part3:
                part3 = part3.strip()
                if _DEPT_KEYWORDS.search(part3):
                    department = part3
                elif not title and _TITLE_KEYWORDS.search(part3):
                    title = part3
            continue

        # Check if the line looks like a title
        if not title and _TITLE_KEYWORDS.search(line) and len(line) < 60:
            # Avoid lines that are clearly email addresses or URLs
            if "@" not in line and "http" not in line.lower():
                title = line

        # Check if the line looks like a department
        if not department and _DEPT_KEYWORDS.search(line) and len(line) < 60:
            if "@" not in line and "http" not in line.lower():
                department = line

    return title, department


async def parse_email_signatures(session: AsyncSession) -> dict:
    """Extract title and department from email signatures for people who lack them.

    Uses regex pattern matching (no LLM cost). One-time per person: skips
    people who already have a title set.

    Returns:
        dict with keys: signatures_parsed, titles_found, departments_found
    """
    # Find people without title who have sent emails
    people_stmt = select(Person).where(
        Person.title.is_(None) | (Person.title == ""),
        Person.email.isnot(None),
    )
    result = await session.execute(people_stmt)
    people_without_title = list(result.scalars().all())

    if not people_without_title:
        logger.info("Signature parsing: no people without title found")
        return {"signatures_parsed": 0, "titles_found": 0, "departments_found": 0}

    signatures_parsed = 0
    titles_found = 0
    departments_found = 0

    for person in people_without_title:
        # Find their most recent sent email (where they are the sender)
        email_stmt = (
            select(Email.body_text)
            .where(Email.sender_id == person.id, Email.body_text.isnot(None))
            .order_by(Email.datetime_.desc())
            .limit(1)
        )
        email_result = await session.execute(email_stmt)
        body_text = email_result.scalar_one_or_none()

        if not body_text:
            continue

        signatures_parsed += 1
        title, department = _extract_title_dept_from_signature(body_text)

        if title:
            await _log_change(session, person.id, "title", person.title, title)
            person.title = title
            titles_found += 1

            # Also update seniority based on new title
            new_seniority = infer_seniority_from_title(title)
            if new_seniority != "unknown" and person.seniority == "unknown":
                await _log_change(session, person.id, "seniority", person.seniority, new_seniority)
                person.seniority = new_seniority

        if department and not person.org:
            await _log_change(session, person.id, "org", person.org, department)
            person.org = department
            departments_found += 1

    if titles_found or departments_found:
        await session.flush()

    logger.info(
        "Signature parsing: parsed %d signatures, found %d titles, %d departments",
        signatures_parsed,
        titles_found,
        departments_found,
    )
    return {
        "signatures_parsed": signatures_parsed,
        "titles_found": titles_found,
        "departments_found": departments_found,
    }


async def infer_departments_from_teams(session: AsyncSession) -> dict:
    """Use Teams membership data as a direct department signal.

    Each Team often maps to a department or workgroup. Creates/updates
    Department records from Team data and links people to departments
    based on team membership.

    Returns:
        dict with keys: departments_created, people_linked
    """
    # Load all teams
    teams_stmt = select(Team)
    teams_result = await session.execute(teams_stmt)
    teams = list(teams_result.scalars().all())

    if not teams:
        logger.info("Teams department inference: no teams found")
        return {"departments_created": 0, "people_linked": 0}

    # Load existing departments (match by name to avoid duplicates)
    dept_stmt = select(Department)
    dept_result = await session.execute(dept_stmt)
    existing_depts = {d.name.lower(): d for d in dept_result.scalars().all()}

    departments_created = 0
    people_linked = 0

    for team in teams:
        # Check if a department already exists matching this team name
        dept_key = team.name.lower().strip()
        dept = existing_depts.get(dept_key)

        if not dept:
            # Create a new department from team data
            dept = Department(
                name=team.name,
                description=team.description or f"Department inferred from Teams team: {team.name}",
                source="teams",
                confidence=0.8,
            )
            session.add(dept)
            await session.flush()
            existing_depts[dept_key] = dept
            departments_created += 1

        # Get team members
        members_stmt = (
            select(TeamMembership.person_id)
            .where(TeamMembership.team_id == team.id)
        )
        members_result = await session.execute(members_stmt)
        member_ids = [row for row in members_result.scalars().all()]

        if not member_ids:
            continue

        # Load people who don't have a department yet
        people_stmt = select(Person).where(
            Person.id.in_(member_ids),
            Person.department_id.is_(None),
        )
        people_result = await session.execute(people_stmt)
        people_to_link = list(people_result.scalars().all())

        for person in people_to_link:
            await _log_change(session, person.id, "department_id", None, str(dept.id))
            person.department_id = dept.id
            people_linked += 1

    if departments_created or people_linked:
        await session.flush()

    logger.info(
        "Teams department inference: created %d departments, linked %d people",
        departments_created,
        people_linked,
    )
    return {"departments_created": departments_created, "people_linked": people_linked}


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

    # Email-based signals
    cc_gravity_stats = await compute_cc_gravity(session)
    signature_stats = await parse_email_signatures(session)
    teams_dept_stats = await infer_departments_from_teams(session)

    await session.commit()

    # Generate LLM suggestions for needs-review people
    suggestions_count = await generate_people_suggestions(session)

    await session.commit()

    stats = {
        "managers_inferred": managers_inferred,
        "seniority_updated": seniority_updated,
        "departments_created": departments_created,
        "cc_gravity_people_updated": cc_gravity_stats["people_updated"],
        "signatures_parsed": signature_stats["signatures_parsed"],
        "titles_found": signature_stats["titles_found"],
        "teams_departments_created": teams_dept_stats["departments_created"],
        "teams_people_linked": teams_dept_stats["people_linked"],
        "llm_suggestions_generated": suggestions_count,
    }
    logger.info("Org inference complete: %s", stats)
    return stats


async def generate_people_suggestions(session: AsyncSession, limit: int = 20) -> int:
    """Generate LLM suggestions for needs-review people without existing suggestions.

    Gathers context from emails and meetings, calls Haiku to suggest
    title, role, seniority, and department. Writes to Person.llm_suggestion.
    """
    import json

    import anthropic

    # Find people needing suggestions
    stmt = (
        select(Person)
        .where(Person.needs_review.is_(True), Person.llm_suggestion.is_(None))
        .order_by(Person.last_seen.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    people = list(result.scalars().all())

    if not people:
        return 0

    client = anthropic.AsyncAnthropic()
    count = 0

    # Get department names for context
    dept_stmt = select(Department.id, Department.name)
    dept_result = await session.execute(dept_stmt)
    dept_map = {row[0]: row[1] for row in dept_result.all()}
    dept_names = list(dept_map.values())

    for person in people:
        try:
            # Gather context: emails they sent or received
            context_parts = [f"Name: {person.name}"]
            if person.email:
                context_parts.append(f"Email: {person.email}")
            if person.title:
                context_parts.append(f"Current title: {person.title}")
            if person.org:
                context_parts.append(f"Current org: {person.org}")

            # Recent email subjects involving this person
            email_stmt = (
                select(Email.subject, Email.intent)
                .where(Email.sender_id == person.id)
                .order_by(Email.datetime_.desc())
                .limit(5)
            )
            email_result = await session.execute(email_stmt)
            emails = email_result.all()
            if emails:
                subjects = [f"- {e[0]} (intent: {e[1]})" for e in emails if e[0]]
                if subjects:
                    context_parts.append("Recent emails sent:\n" + "\n".join(subjects))

            # Meeting titles they attended
            meeting_stmt = (
                select(Meeting.title)
                .join(MeetingAttendee, Meeting.id == MeetingAttendee.meeting_id)
                .where(MeetingAttendee.person_id == person.id)
                .order_by(Meeting.start_time.desc())
                .limit(5)
            )
            meeting_result = await session.execute(meeting_stmt)
            meetings = [r[0] for r in meeting_result.all() if r[0]]
            if meetings:
                context_parts.append("Recent meetings:\n" + "\n".join(f"- {m}" for m in meetings))

            context = "\n".join(context_parts)

            prompt = f"""\
Based on the following information about a person, suggest their likely:
- title (job title)
- role (functional role like "engineer", "manager", "analyst")
- seniority (one of: executive, senior, mid, junior, unknown)
- department (one of: {', '.join(dept_names) if dept_names else 'unknown'})
- notes (brief reasoning)

Person info:
{context}

Return a JSON object with keys: title, role, seniority, department, notes.
Return ONLY the JSON, no other text."""

            response = await client.messages.create(
                model="claude-haiku-4-5-20241022",
                max_tokens=300,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()
            # Parse JSON from response
            if "{" in text:
                json_str = text[text.index("{"):text.rindex("}") + 1]
                suggestion = json.loads(json_str)
                person.llm_suggestion = suggestion
                count += 1
                logger.info("Generated LLM suggestion for person %d (%s)", person.id, person.name)

        except Exception:
            logger.debug("Failed to generate suggestion for person %d", person.id, exc_info=True)
            continue

    return count
