"""Entity resolution — match extracted people against the People table.

Uses a 3-step approach:
1. Exact email match
2. Fuzzy name match via rapidfuzz (threshold 85)
3. Create new Person with needs_review=True if no match
"""

import logging
from datetime import datetime, timezone

from rapidfuzz import fuzz
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import Person
from aegis.db.repositories import get_all_people

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 85  # minimum score for rapidfuzz name match


async def resolve_extracted_entities(
    session: AsyncSession,
    meeting_id: int,
    extraction: dict,
) -> None:
    """Resolve person references in extraction against People table.

    Modifies extraction dict in-place, adding a _resolved_people mapping
    of name -> person_id that store_meeting_extraction uses.

    Called from pipeline.py resolve_node.
    """
    all_people = await get_all_people(session)

    # Build lookup structures
    email_index: dict[str, Person] = {}
    name_list: list[tuple[str, Person]] = []
    for person in all_people:
        if person.email:
            email_index[person.email.lower()] = person
        name_list.append((person.name.lower(), person))
        # Also index aliases
        if person.aliases:
            for alias in person.aliases:
                name_list.append((alias.lower(), person))

    resolved_people: dict[str, int] = {}
    now = datetime.now(timezone.utc)

    extracted_people = extraction.get("people", [])
    for ep in extracted_people:
        name = ep.get("name", "")
        email = ep.get("email")

        if not name:
            continue

        person = None

        # Step 1: exact email match
        if email:
            person = email_index.get(email.lower())

        # Step 2: fuzzy name match
        if person is None:
            best_score = 0
            best_match = None
            for candidate_name, candidate_person in name_list:
                score = fuzz.ratio(name.lower(), candidate_name)
                if score > best_score and score >= FUZZY_THRESHOLD:
                    best_score = score
                    best_match = candidate_person
            person = best_match

        # Step 3: create new person if no match
        if person is None:
            person = Person(
                name=name,
                email=email,
                source="meeting",
                needs_review=True,
                first_seen=now,
                last_seen=now,
                interaction_count=1,
                confidence=0.5,
            )
            session.add(person)
            await session.flush()  # get the id
            logger.info(
                "Created new person '%s' (id=%d) from meeting %d",
                name, person.id, meeting_id,
            )
        else:
            # Update existing person
            await session.execute(
                update(Person)
                .where(Person.id == person.id)
                .values(
                    last_seen=now,
                    interaction_count=Person.interaction_count + 1,
                )
            )

        resolved_people[name] = person.id

    await session.commit()

    # Store resolved mapping on extraction dict for store_meeting_extraction
    extraction["_resolved_people"] = resolved_people

    logger.info(
        "Resolved %d people for meeting %d (%d new)",
        len(resolved_people),
        meeting_id,
        sum(1 for ep in extracted_people if ep.get("name") and ep["name"] not in
            {p.name for p in all_people}),
    )
