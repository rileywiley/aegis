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

from aegis.config import get_settings
from aegis.db.models import Person
from aegis.db.repositories import get_all_people

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 80  # minimum score for rapidfuzz name match (lowered for partial names)


def _is_external_email(email: str | None) -> bool:
    """Check if an email address belongs to an external domain."""
    if not email or "@" not in email:
        return False
    settings = get_settings()
    org_domains = [d.strip().lower() for d in settings.org_email_domains.split(",") if d.strip()]
    if not org_domains:
        return False
    domain = email.split("@", 1)[1].lower()
    return domain not in org_domains


async def resolve_extracted_entities(
    session: AsyncSession,
    meeting_id: int,
    extraction: dict,
) -> None:
    """Resolve person references in extraction against People table.

    Resolves ALL person names found anywhere in the extraction:
    - people[].name
    - action_items[].assignee
    - decisions[].decided_by
    - commitments[].committer / recipient

    Modifies extraction dict in-place, adding a _resolved_people mapping
    of name -> person_id that store_meeting_extraction uses.
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

    # Collect ALL unique names that need resolution from the extraction
    names_to_resolve: dict[str, str | None] = {}  # name -> email (if available)

    for ep in extraction.get("people", []):
        name = ep.get("name", "")
        if name:
            names_to_resolve[name] = ep.get("email")

    for ai in extraction.get("action_items", []):
        if ai.get("assignee"):
            names_to_resolve.setdefault(ai["assignee"], None)

    for dec in extraction.get("decisions", []):
        if dec.get("decided_by"):
            names_to_resolve.setdefault(dec["decided_by"], None)

    for com in extraction.get("commitments", []):
        if com.get("committer"):
            names_to_resolve.setdefault(com["committer"], None)
        if com.get("recipient"):
            names_to_resolve.setdefault(com["recipient"], None)

    resolved_people: dict[str, int] = {}
    now = datetime.now(timezone.utc)

    for name, email in names_to_resolve.items():
        if not name:
            continue

        person = None

        # Step 1: exact email match
        if email:
            person = email_index.get(email.lower())

        # Step 2: fuzzy name match (also try partial_ratio for "James" vs "James Park")
        if person is None:
            best_score = 0
            best_match = None
            for candidate_name, candidate_person in name_list:
                # Use token_set_ratio for better partial name matching
                score = max(
                    fuzz.ratio(name.lower(), candidate_name),
                    fuzz.partial_ratio(name.lower(), candidate_name),
                )
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
                is_external=_is_external_email(email),
                needs_review=True,
                first_seen=now,
                last_seen=now,
                interaction_count=1,
                confidence=0.5,
            )
            session.add(person)
            await session.flush()
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
        "Resolved %d people for meeting %d (%d from DB, %d new)",
        len(resolved_people),
        meeting_id,
        sum(1 for n in resolved_people if n in {p.name for p in all_people}),
        sum(1 for n in resolved_people if n not in {p.name for p in all_people}),
    )
