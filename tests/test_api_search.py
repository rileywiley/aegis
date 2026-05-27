"""Tests for GET /api/search — Helios attachment picker JSON endpoint.

Reference: HELIOS_BUILD_PLAN.md Track 4H.2.

The endpoint reads from People, Workstreams, EmailAsk, and ChatAsk via
``AsyncSession.execute``. We mock the session so the tests run without
Postgres — same pattern as ``test_voice_notes_endpoints.py`` and
``test_meetings_upcoming.py``. Each test pre-loads the mock session with
the result rows it expects to be returned in execution order.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from aegis.db.engine import get_session
from aegis.main import app


# ── Helpers ────────────────────────────────────────────────


def _make_person(
    *,
    id: int = 1,
    name: str = "Jane Doe",
    email: str | None = "jane@example.com",
    title: str | None = "VP Engineering",
    role: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(id=id, name=name, email=email, title=title, role=role)


def _make_workstream(
    *,
    id: int = 17,
    name: str = "Q3 Migration",
    description: str | None = "Move services to k8s",
    status: str = "active",
) -> SimpleNamespace:
    return SimpleNamespace(id=id, name=name, description=description, status=status)


def _make_email_ask(
    *,
    id: int = 5,
    description: str = "Approve PRD by Friday",
) -> SimpleNamespace:
    return SimpleNamespace(id=id, description=description)


def _make_email(
    *,
    id: int = 1,
    subject: str | None = "PRD review",
) -> SimpleNamespace:
    return SimpleNamespace(id=id, subject=subject)


def _make_chat_ask(
    *,
    id: int = 9,
    description: str = "Approve the chat ask",
) -> SimpleNamespace:
    return SimpleNamespace(id=id, description=description)


def _make_chat_message(
    *,
    id: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(id=id)


def _scalars_result(values: list) -> MagicMock:
    """Mock execute() return whose .scalars().all() yields ``values``."""
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = values
    result.scalars.return_value = scalars
    return result


def _rows_result(rows: list) -> MagicMock:
    """Mock execute() return whose .all() yields ``rows``.

    Each row is a tuple of (orm_obj, ...). Used by the ask searches that
    do JOINs and pull multiple objects per row.
    """
    result = MagicMock()
    result.all.return_value = rows
    return result


@pytest.fixture
def session_mock() -> AsyncMock:
    """Build a fresh AsyncMock session whose .execute() can be queued."""
    session = AsyncMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture
def client(session_mock):
    """TestClient with the DB session dependency overridden to ``session_mock``."""

    async def _fake_session():
        yield session_mock

    app.dependency_overrides[get_session] = _fake_session
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


def _set_executes(session_mock: AsyncMock, *return_values) -> None:
    """Queue ``session.execute`` results in the order they will be called."""
    session_mock.execute.side_effect = list(return_values)


# ═══════════════════════════════════════════════════════════
# Empty-input handling
# ═══════════════════════════════════════════════════════════


class TestEmptyInput:
    def test_empty_q_returns_empty_list(self, client, session_mock):
        """q="" → 200, []. No DB calls."""
        resp = client.get("/api/search?q=")
        assert resp.status_code == 200
        assert resp.json() == []
        # No DB hits when q is empty.
        session_mock.execute.assert_not_called()

    def test_whitespace_q_returns_empty_list(self, client, session_mock):
        """q="   " → 200, []. No DB calls."""
        resp = client.get("/api/search?q=%20%20%20")
        assert resp.status_code == 200
        assert resp.json() == []
        session_mock.execute.assert_not_called()

    def test_missing_q_returns_empty_list(self, client, session_mock):
        """q omitted entirely → 200, [] (default is empty string, not 422)."""
        resp = client.get("/api/search")
        assert resp.status_code == 200
        assert resp.json() == []


# ═══════════════════════════════════════════════════════════
# Per-type matching
# ═══════════════════════════════════════════════════════════


class TestPersonSearch:
    def test_search_person_by_name(self, client, session_mock):
        person = _make_person(id=42, name="Jane Doe", title="VP Engineering")
        _set_executes(
            session_mock,
            _scalars_result([person]),  # people query
            _scalars_result([]),         # workstreams query
            _rows_result([]),            # email_asks query
            _rows_result([]),            # chat_asks query
        )

        resp = client.get("/api/search?q=Jane")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["type"] == "person"
        assert data[0]["id"] == 42
        assert data[0]["label"] == "Jane Doe"
        # Snippet should be the title.
        assert "VP Engineering" in data[0]["snippet"]


class TestWorkstreamSearch:
    def test_search_workstream_by_name(self, client, session_mock):
        ws = _make_workstream(id=17, name="Q3 Migration", status="active")
        _set_executes(
            session_mock,
            _scalars_result([]),         # people
            _scalars_result([ws]),        # workstreams
            _rows_result([]),             # email_asks
            _rows_result([]),             # chat_asks
        )

        resp = client.get("/api/search?q=Q3")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["type"] == "workstream"
        assert data[0]["id"] == 17
        assert data[0]["label"] == "Q3 Migration"
        # Snippet starts with capitalized status.
        assert data[0]["snippet"].startswith("Active")


class TestAskSearch:
    def test_search_email_ask_by_description(self, client, session_mock):
        ask = _make_email_ask(id=5, description="Approve PRD by Friday")
        email = _make_email(id=1, subject="PRD review")
        _set_executes(
            session_mock,
            _scalars_result([]),                  # people
            _scalars_result([]),                  # workstreams
            _rows_result([(ask, email)]),          # email_asks (JOINed)
            _rows_result([]),                      # chat_asks
        )

        resp = client.get("/api/search?q=Approve")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["type"] == "ask"
        assert data[0]["id"] == 5
        assert data[0]["label"] == "Approve PRD by Friday"
        # Snippet should hint at the source.
        assert "email" in data[0]["snippet"].lower()

    def test_search_chat_ask_by_description(self, client, session_mock):
        ask = _make_chat_ask(id=9, description="Approve the chat ask")
        msg = _make_chat_message(id=1)
        _set_executes(
            session_mock,
            _scalars_result([]),                                   # people
            _scalars_result([]),                                   # workstreams
            _rows_result([]),                                      # email_asks
            _rows_result([(ask, msg, "John")]),                    # chat_asks
        )

        resp = client.get("/api/search?q=Approve")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["type"] == "ask"
        assert data[0]["id"] == 9
        # Snippet should hint Teams/chat origin.
        assert "John" in data[0]["snippet"] or "Teams" in data[0]["snippet"]


# ═══════════════════════════════════════════════════════════
# `types` filter
# ═══════════════════════════════════════════════════════════


class TestTypesFilter:
    def test_types_person_only_runs_only_people_query(self, client, session_mock):
        person = _make_person(id=1, name="Alice")
        _set_executes(session_mock, _scalars_result([person]))

        resp = client.get("/api/search?q=Alice&types=person")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["type"] == "person"
        # Only ONE execute call (people only).
        assert session_mock.execute.await_count == 1

    def test_types_workstream_and_ask_no_person_results(self, client, session_mock):
        """types=workstream,ask → no person results, even if 'matching' people exist."""
        # Only workstream + ask queries should fire — no people query.
        ws = _make_workstream(id=2, name="Alpha")
        _set_executes(
            session_mock,
            _scalars_result([ws]),       # workstreams
            _rows_result([]),             # email_asks
            _rows_result([]),             # chat_asks
        )

        resp = client.get("/api/search?q=Alpha&types=workstream,ask")
        assert resp.status_code == 200
        data = resp.json()
        types_returned = {r["type"] for r in data}
        assert "person" not in types_returned
        assert "workstream" in types_returned
        # Three executes (workstreams + email_asks + chat_asks). NOT four.
        assert session_mock.execute.await_count == 3

    def test_invalid_type_value_silently_filtered(self, client, session_mock):
        """types=foo,person → ignores 'foo', still searches people."""
        person = _make_person(id=1, name="Bob")
        _set_executes(session_mock, _scalars_result([person]))

        resp = client.get("/api/search?q=Bob&types=foo,person")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["type"] == "person"

    def test_all_invalid_types_returns_empty(self, client, session_mock):
        """types=foo,bar → 200, [], no DB calls."""
        resp = client.get("/api/search?q=anything&types=foo,bar")
        assert resp.status_code == 200
        assert resp.json() == []
        session_mock.execute.assert_not_called()


# ═══════════════════════════════════════════════════════════
# Limit handling
# ═══════════════════════════════════════════════════════════


class TestLimit:
    def test_limit_caps_results(self, client, session_mock):
        """limit=2 → at most 2 results returned."""
        people = [_make_person(id=i, name=f"Person{i}") for i in range(1, 6)]
        _set_executes(
            session_mock,
            _scalars_result(people),  # people
            _scalars_result([]),       # workstreams
            _rows_result([]),          # email_asks
            _rows_result([]),          # chat_asks
        )

        resp = client.get("/api/search?q=Person&limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) <= 2

    def test_limit_clamped_to_50(self, client, session_mock):
        """limit=999 → effectively clamped to 50; even with many rows we cap."""
        # 60 people — final response must be <= 50.
        people = [_make_person(id=i, name=f"Sam{i}") for i in range(1, 61)]
        _set_executes(
            session_mock,
            _scalars_result(people),
            _scalars_result([]),
            _rows_result([]),
            _rows_result([]),
        )

        resp = client.get("/api/search?q=Sam&limit=999")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) <= 50

    def test_limit_minimum_one(self, client, session_mock):
        """limit=0 → clamped to 1 (not 0)."""
        people = [_make_person(id=i, name=f"P{i}") for i in range(1, 4)]
        _set_executes(
            session_mock,
            _scalars_result(people),
            _scalars_result([]),
            _rows_result([]),
            _rows_result([]),
        )

        resp = client.get("/api/search?q=P&limit=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert len(data) <= 1


# ═══════════════════════════════════════════════════════════
# Result shape & content type
# ═══════════════════════════════════════════════════════════


class TestResponseShape:
    def test_each_result_has_required_keys(self, client, session_mock):
        person = _make_person(id=1, name="Alice")
        ws = _make_workstream(id=2, name="Alpha")
        ask = _make_email_ask(id=3, description="Alpha approval")
        email = _make_email(id=10, subject="Alpha")
        _set_executes(
            session_mock,
            _scalars_result([person]),
            _scalars_result([ws]),
            _rows_result([(ask, email)]),
            _rows_result([]),
        )

        resp = client.get("/api/search?q=Al")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        for item in data:
            assert set(item.keys()) == {"type", "id", "label", "snippet"}
            assert isinstance(item["type"], str)
            assert isinstance(item["id"], int)
            assert isinstance(item["label"], str)
            assert isinstance(item["snippet"], str)

    def test_returns_json_content_type(self, client, session_mock):
        """Content-Type must be JSON (NOT text/html)."""
        _set_executes(
            session_mock,
            _scalars_result([]),
            _scalars_result([]),
            _rows_result([]),
            _rows_result([]),
        )

        resp = client.get("/api/search?q=zzz")
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert "application/json" in ct
        assert "text/html" not in ct
