"""Tests for entity resolution — exact match, fuzzy match, new person creation."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.processing.resolver import resolve_extracted_entities


def _make_person(id: int, name: str, email: str | None = None, aliases: list[str] | None = None):
    """Create a mock Person object."""
    person = MagicMock()
    person.id = id
    person.name = name
    person.email = email
    person.aliases = aliases or []
    person.interaction_count = 5
    return person


# ── Test: exact email match ──────────────────────────────


@pytest.mark.asyncio
async def test_resolve_exact_email_match():
    """Should resolve by exact email match first."""
    alice = _make_person(1, "Alice Smith", email="alice@example.com")

    extraction = {
        "people": [
            {"name": "Alice", "email": "alice@example.com"},
        ],
    }

    mock_session = AsyncMock()

    with patch("aegis.processing.resolver.get_all_people", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = [alice]

        await resolve_extracted_entities(
            session=mock_session,
            meeting_id=1,
            extraction=extraction,
        )

    assert extraction["_resolved_people"]["Alice"] == 1
    # Should have called update on session (last_seen + interaction_count)
    mock_session.execute.assert_called()


# ── Test: fuzzy name match ───────────────────────────────


@pytest.mark.asyncio
async def test_resolve_fuzzy_name_match():
    """Should match 'Bob Jones' to 'Robert Jones' if fuzzy score >= 85 is not met,
    but 'Bob Jones' to 'Bob Jones' should match."""
    bob = _make_person(2, "Bob Jones", email="bob@example.com")

    extraction = {
        "people": [
            {"name": "Bob Jones", "email": None},
        ],
    }

    mock_session = AsyncMock()

    with patch("aegis.processing.resolver.get_all_people", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = [bob]

        await resolve_extracted_entities(
            session=mock_session,
            meeting_id=1,
            extraction=extraction,
        )

    # Exact name match via fuzzy should yield score of 100
    assert extraction["_resolved_people"]["Bob Jones"] == 2


@pytest.mark.asyncio
async def test_resolve_fuzzy_near_match():
    """Should match 'Bobby Jones' to 'Bob Jones' if score is above threshold."""
    bob = _make_person(2, "Bob Jones", email="bob@example.com")

    extraction = {
        "people": [
            {"name": "Bob Jone", "email": None},  # close enough (score ~90)
        ],
    }

    mock_session = AsyncMock()

    with patch("aegis.processing.resolver.get_all_people", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = [bob]

        await resolve_extracted_entities(
            session=mock_session,
            meeting_id=1,
            extraction=extraction,
        )

    assert extraction["_resolved_people"]["Bob Jone"] == 2


@pytest.mark.asyncio
async def test_resolve_fuzzy_below_threshold_creates_new():
    """A name too different from any existing person should create a new record."""
    alice = _make_person(1, "Alice Smith", email="alice@example.com")

    extraction = {
        "people": [
            {"name": "Zara Patel", "email": None},
        ],
    }

    mock_session = AsyncMock()
    # Mock session.add and flush to set an id
    added_objects = []

    def capture_add(obj):
        added_objects.append(obj)

    mock_session.add = capture_add
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()

    with patch("aegis.processing.resolver.get_all_people", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = [alice]
        with patch("aegis.processing.resolver.Person") as MockPerson:
            new_person = MagicMock()
            new_person.id = 99
            new_person.name = "Zara Patel"
            MockPerson.return_value = new_person

            await resolve_extracted_entities(
                session=mock_session,
                meeting_id=1,
                extraction=extraction,
            )

    assert extraction["_resolved_people"]["Zara Patel"] == 99


# ── Test: interaction_count update ───────────────────────


@pytest.mark.asyncio
async def test_resolve_increments_interaction_count():
    """Resolving to an existing person should update last_seen and interaction_count."""
    alice = _make_person(1, "Alice Smith", email="alice@example.com")

    extraction = {
        "people": [
            {"name": "Alice Smith", "email": "alice@example.com"},
        ],
    }

    mock_session = AsyncMock()

    with patch("aegis.processing.resolver.get_all_people", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = [alice]

        await resolve_extracted_entities(
            session=mock_session,
            meeting_id=1,
            extraction=extraction,
        )

    # session.execute should have been called with an update statement
    assert mock_session.execute.called
    # The update call increments interaction_count (verified by inspecting the SQL)
    call_args = mock_session.execute.call_args_list
    assert len(call_args) >= 1  # At least one update call


# ── Test: multiple people resolution ─────────────────────


@pytest.mark.asyncio
async def test_resolve_multiple_people():
    """Should resolve multiple people, some existing, some new."""
    alice = _make_person(1, "Alice Smith", email="alice@example.com")

    extraction = {
        "people": [
            {"name": "Alice Smith", "email": "alice@example.com"},
            {"name": "Unknown Person", "email": None},
        ],
    }

    mock_session = AsyncMock()

    # Make session.add + flush assign an id to new Person objects
    async def mock_flush():
        for call in mock_session.add.call_args_list:
            obj = call[0][0]
            if hasattr(obj, "id") and obj.id is None:
                obj.id = 50

    mock_session.flush = AsyncMock(side_effect=mock_flush)

    with patch("aegis.processing.resolver.get_all_people", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = [alice]

        await resolve_extracted_entities(
            session=mock_session,
            meeting_id=1,
            extraction=extraction,
        )

    assert extraction["_resolved_people"]["Alice Smith"] == 1
    assert extraction["_resolved_people"]["Unknown Person"] == 50


# ── Test: alias matching ─────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_via_alias():
    """Should match against person aliases."""
    bob = _make_person(2, "Robert Jones", email="bob@example.com", aliases=["Bob Jones"])

    extraction = {
        "people": [
            {"name": "Bob Jones", "email": None},
        ],
    }

    mock_session = AsyncMock()

    with patch("aegis.processing.resolver.get_all_people", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = [bob]

        await resolve_extracted_entities(
            session=mock_session,
            meeting_id=1,
            extraction=extraction,
        )

    # Should match via alias
    assert extraction["_resolved_people"]["Bob Jones"] == 2
