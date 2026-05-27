"""Entity resolution — match extracted people against the People table.

Uses a 3-step approach:
1. Exact email match
2. Fuzzy name match via rapidfuzz (threshold 85)
3. Create new Person with needs_review=True if no match
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from rapidfuzz import fuzz
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import Person
from aegis.db.repositories import (
    get_all_asks,
    get_all_people,
    get_workstreams,
)

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 80  # minimum score for rapidfuzz name match (lowered for partial names)

# Lower-cased common English words we never want to treat as entity name spans.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "to", "of", "for",
    "with", "on", "in", "at", "by", "as", "is", "it", "this",
    "that", "be", "are", "was", "were", "have", "has", "had",
    "do", "does", "did", "will", "would", "should", "could",
    "may", "might", "can", "i", "me", "we", "they", "you",
    "he", "she", "him", "her", "our", "your", "their", "his",
    "hers", "from", "about", "team", "follow", "up",
})


@dataclass
class ResolverMatch:
    """A single span-to-entity match returned by ``resolve_text``."""

    target_type: Literal["person", "workstream", "email_ask", "chat_ask"]
    target_id: int
    label: str
    confidence: float
    span: str


async def resolve_text(
    session: AsyncSession, text: str
) -> list[ResolverMatch]:
    """Match free-text against People, Workstreams, and Asks.

    Used by ``POST /api/voice-notes/preview-attachments`` (Wave 3K). The
    return value is a flat list of matches with per-entity scores in
    [0.0, 1.0]. The endpoint applies its own confidence threshold to
    decide which matches become "suggested" attachments vs. "see more"
    weak matches.

    Strategy:
        1. Build a candidate label list from all People (name + aliases),
           Workstreams (name), and Asks (description).
        2. For each candidate, run rapidfuzz ``token_set_ratio`` against
           the full transcript and ``partial_ratio`` for short queries.
        3. Keep matches above a small floor (score >= 50) so the caller
           can still surface near-misses behind a "see more" UI.
        4. Confidence is the rapidfuzz score / 100, capped to [0, 1].
        5. Span is the candidate label that triggered the match (the
           best human-readable representation we have without doing
           full NER).

    Returns: list of ``ResolverMatch``, deduplicated by
    (target_type, target_id) — a person/workstream/ask appears at most
    once with its best score.
    """
    if not text or not text.strip():
        return []

    text_lower = text.lower()
    matches: dict[tuple[str, int], ResolverMatch] = {}

    # ── People ────────────────────────────────────────────────
    # Pre-compute the word tokens in the transcript once — used to
    # gate short candidates (first names <= 5 chars). Without this,
    # rapidfuzz partial_ratio happily returns 100 for "Tom" matching
    # any transcript containing "tomorrow" / "atom" / "custom" / ...
    # which caused ~250 spurious attachments per 60s voice note.
    text_tokens: set[str] = set(re.findall(r"\b[\w'-]+\b", text_lower))
    people = await get_all_people(session)
    for person in people:
        candidates: list[str] = [person.name]
        if person.aliases:
            candidates.extend(person.aliases)
        # Try first-name match too — voice notes commonly say "Sarah"
        # not "Sarah Lin".
        first_name = person.name.split(" ", 1)[0] if person.name else ""
        if first_name and first_name not in candidates:
            candidates.append(first_name)

        best_score = 0.0
        best_span = ""
        for cand in candidates:
            cand_lower = cand.lower().strip()
            if not cand_lower or cand_lower in _STOPWORDS:
                continue
            # Short candidates (<= 5 chars) require a whole-token match:
            # rapidfuzz's partial_ratio aligns substrings anywhere in
            # the longer string, so "Tom" otherwise matches "tomorrow"
            # at score 100. Real boundary-aware fuzzing isn't available
            # without going to spaCy/NER, so we gate the cheap path.
            if len(cand_lower) <= 5:
                if cand_lower not in text_tokens:
                    continue
                score = 100.0
            else:
                # Whole-word substring is a strong signal for longer names.
                score = max(
                    fuzz.token_set_ratio(cand_lower, text_lower),
                    fuzz.partial_ratio(cand_lower, text_lower),
                )
            if score > best_score:
                best_score = score
                best_span = cand
        if best_score >= 50:
            key = ("person", person.id)
            confidence = min(best_score / 100.0, 1.0)
            existing = matches.get(key)
            if existing is None or confidence > existing.confidence:
                matches[key] = ResolverMatch(
                    target_type="person",
                    target_id=person.id,
                    label=person.name,
                    confidence=confidence,
                    span=best_span,
                )

    # ── Workstreams ───────────────────────────────────────────
    workstreams = await get_workstreams(session)
    for ws in workstreams:
        if not ws.name:
            continue
        score = max(
            fuzz.token_set_ratio(ws.name.lower(), text_lower),
            fuzz.partial_ratio(ws.name.lower(), text_lower),
        )
        if score >= 50:
            key = ("workstream", ws.id)
            confidence = min(score / 100.0, 1.0)
            existing = matches.get(key)
            if existing is None or confidence > existing.confidence:
                matches[key] = ResolverMatch(
                    target_type="workstream",
                    target_id=ws.id,
                    label=ws.name,
                    confidence=confidence,
                    span=ws.name,
                )

    # ── Asks ──────────────────────────────────────────────────
    # Only consider open asks — closed ones are unlikely to be referenced
    # in a fresh voice note. ``get_all_asks`` returns dicts.
    try:
        ask_rows, _ = await get_all_asks(session, status="open", per_page=200)
    except Exception:  # noqa: BLE001 - resilient to schema shifts
        ask_rows = []
    for ask in ask_rows:
        desc = ask.get("description") or ""
        if not desc:
            continue
        # Asks are long descriptions; partial_ratio is more useful here.
        score = fuzz.partial_ratio(desc.lower(), text_lower)
        if score >= 70:  # higher floor — long descriptions match too easily
            # Critical #4: email_asks and chat_asks have separate sequences,
            # so the same numeric id can appear in both tables. Split the
            # target_type so attachments survive correctly.
            source = ask.get("source_type") or "email"
            ask_target_type = "email_ask" if source == "email" else "chat_ask"
            key = (ask_target_type, int(ask["id"]))
            confidence = min(score / 100.0, 1.0)
            existing = matches.get(key)
            if existing is None or confidence > existing.confidence:
                matches[key] = ResolverMatch(
                    target_type=ask_target_type,
                    target_id=int(ask["id"]),
                    label=(desc[:60] + "…") if len(desc) > 60 else desc,
                    confidence=confidence,
                    span=desc[:60],
                )

    return sorted(matches.values(), key=lambda m: m.confidence, reverse=True)


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
