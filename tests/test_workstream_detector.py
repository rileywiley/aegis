"""Tests for workstream detection — Layers 1-3 + lifecycle management."""

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.processing.workstream_detector import (
    UnassignedItem,
    _can_cluster_together,
    _cluster_confidence,
    _cluster_items,
    _source_type_count,
    cosine_similarity,
    manage_workstream_lifecycle,
    run_workstream_assignment,
)


# ── Cosine Similarity Tests ────────────────────────────────


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 2.0, 3.0]
        b = [-1.0, -2.0, -3.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_empty_vectors(self):
        assert cosine_similarity([], []) == 0.0

    def test_different_length_vectors(self):
        assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0

    def test_zero_vector(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_known_similarity(self):
        a = [1.0, 0.0]
        b = [1.0, 1.0]
        expected = 1.0 / math.sqrt(2)
        assert cosine_similarity(a, b) == pytest.approx(expected, rel=1e-6)


# ── Clustering Helpers ─────────────────────────────────────


def _make_item(
    item_type: str = "email",
    item_id: int = 1,
    text: str = "test",
    embedding: list[float] | None = None,
    department_id: int | None = None,
    participant_ids: list[int] | None = None,
) -> UnassignedItem:
    return UnassignedItem(
        item_type=item_type,
        item_id=item_id,
        text=text,
        embedding=embedding,
        department_id=department_id,
        participant_ids=participant_ids,
    )


class TestCanClusterTogether:
    def test_same_department(self):
        a = _make_item(department_id=1)
        b = _make_item(department_id=1)
        assert _can_cluster_together(a, b) is True

    def test_no_department_info(self):
        a = _make_item(department_id=None)
        b = _make_item(department_id=1)
        assert _can_cluster_together(a, b) is True

    def test_different_departments_no_shared_participants(self):
        a = _make_item(department_id=1, participant_ids=[10])
        b = _make_item(department_id=2, participant_ids=[20])
        assert _can_cluster_together(a, b) is False

    def test_different_departments_with_shared_participant(self):
        a = _make_item(department_id=1, participant_ids=[10, 30])
        b = _make_item(department_id=2, participant_ids=[20, 30])
        assert _can_cluster_together(a, b) is True


class TestClusterItems:
    def test_empty_items(self):
        assert _cluster_items([]) == []

    def test_items_without_embeddings(self):
        items = [_make_item(embedding=None) for _ in range(5)]
        assert _cluster_items(items) == []

    def test_identical_embeddings_cluster_together(self):
        emb = [1.0, 0.0, 0.0]
        items = [
            _make_item(item_type="email", item_id=1, embedding=emb),
            _make_item(item_type="meeting", item_id=2, embedding=emb),
            _make_item(item_type="chat_message", item_id=3, embedding=emb),
        ]
        clusters = _cluster_items(items, similarity_threshold=0.6)
        assert len(clusters) == 1
        assert len(clusters[0]) == 3

    def test_dissimilar_items_dont_cluster(self):
        items = [
            _make_item(item_id=1, embedding=[1.0, 0.0, 0.0]),
            _make_item(item_id=2, embedding=[0.0, 1.0, 0.0]),
            _make_item(item_id=3, embedding=[0.0, 0.0, 1.0]),
        ]
        clusters = _cluster_items(items, similarity_threshold=0.6)
        # Orthogonal vectors should not cluster
        assert len(clusters) == 0

    def test_zero_embeddings_excluded(self):
        items = [
            _make_item(item_id=1, embedding=[0.0, 0.0, 0.0]),
            _make_item(item_id=2, embedding=[0.0, 0.0, 0.0]),
        ]
        clusters = _cluster_items(items)
        assert len(clusters) == 0

    def test_org_chart_constraint_blocks_clustering(self):
        """Items from unrelated departments with no shared participants should not cluster."""
        emb = [1.0, 0.5, 0.0]
        items = [
            _make_item(item_id=1, embedding=emb, department_id=1, participant_ids=[10]),
            _make_item(item_id=2, embedding=emb, department_id=2, participant_ids=[20]),
        ]
        clusters = _cluster_items(items, similarity_threshold=0.6)
        # Even though embeddings are identical, org constraint blocks them
        assert len(clusters) == 0


class TestSourceTypeCount:
    def test_single_type(self):
        items = [_make_item(item_type="email") for _ in range(3)]
        assert _source_type_count(items) == 1

    def test_multiple_types(self):
        items = [
            _make_item(item_type="email"),
            _make_item(item_type="meeting"),
            _make_item(item_type="chat_message"),
        ]
        assert _source_type_count(items) == 3


class TestClusterConfidence:
    def test_single_item(self):
        assert _cluster_confidence([_make_item()]) == 0.0

    def test_identical_embeddings(self):
        emb = [1.0, 0.0, 0.0]
        items = [_make_item(embedding=emb), _make_item(embedding=emb)]
        assert _cluster_confidence(items) == pytest.approx(1.0)

    def test_mixed_similarity(self):
        items = [
            _make_item(embedding=[1.0, 0.0]),
            _make_item(embedding=[0.9, 0.1]),
            _make_item(embedding=[0.8, 0.2]),
        ]
        conf = _cluster_confidence(items)
        assert 0.0 < conf < 1.0


# ── Layer 2 Assignment Tests (with mocks) ──────────────────


class TestWorkstreamAssignment:
    @pytest.mark.asyncio
    async def test_no_unassigned_items(self):
        """When no unassigned items exist, return zeros."""
        with patch(
            "aegis.processing.workstream_detector._fetch_unassigned_items",
            new_callable=AsyncMock,
            return_value=[],
        ):
            session = AsyncMock()
            result = await run_workstream_assignment(session)
            assert result == {"items_assigned": 0, "items_unassigned": 0}

    @pytest.mark.asyncio
    async def test_no_active_workstreams(self):
        """When no active workstreams exist, all items are unassigned."""
        items = [_make_item(embedding=[1.0, 0.0, 0.0])]
        with (
            patch(
                "aegis.processing.workstream_detector._fetch_unassigned_items",
                new_callable=AsyncMock,
                return_value=items,
            ),
            patch(
                "aegis.processing.workstream_detector._ensure_embeddings",
                new_callable=AsyncMock,
                return_value=items,
            ),
            patch(
                "aegis.processing.workstream_detector.get_workstreams",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            session = AsyncMock()
            result = await run_workstream_assignment(session)
            assert result["items_unassigned"] == 1

    @pytest.mark.asyncio
    async def test_high_confidence_assignment(self):
        """Items with high similarity get auto-assigned."""
        item_emb = [1.0, 0.0, 0.0]
        items = [_make_item(embedding=item_emb)]

        ws_mock = MagicMock()
        ws_mock.id = 1
        ws_mock.name = "Test WS"
        ws_mock.embedding = [1.0, 0.0, 0.0]  # identical = sim 1.0

        with (
            patch(
                "aegis.processing.workstream_detector._fetch_unassigned_items",
                new_callable=AsyncMock,
                return_value=items,
            ),
            patch(
                "aegis.processing.workstream_detector._ensure_embeddings",
                new_callable=AsyncMock,
                return_value=items,
            ),
            patch(
                "aegis.processing.workstream_detector.get_workstreams",
                new_callable=AsyncMock,
                return_value=[ws_mock],
            ),
            patch(
                "aegis.processing.workstream_detector.link_item_to_workstream",
                new_callable=AsyncMock,
            ) as mock_link,
        ):
            session = AsyncMock()
            result = await run_workstream_assignment(session)
            assert result["items_assigned"] == 1
            mock_link.assert_called_once()

    @pytest.mark.asyncio
    async def test_low_similarity_not_assigned(self):
        """Items with low similarity are not assigned."""
        items = [_make_item(embedding=[1.0, 0.0, 0.0])]

        ws_mock = MagicMock()
        ws_mock.id = 1
        ws_mock.name = "Test WS"
        ws_mock.embedding = [0.0, 1.0, 0.0]  # orthogonal = sim 0.0

        with (
            patch(
                "aegis.processing.workstream_detector._fetch_unassigned_items",
                new_callable=AsyncMock,
                return_value=items,
            ),
            patch(
                "aegis.processing.workstream_detector._ensure_embeddings",
                new_callable=AsyncMock,
                return_value=items,
            ),
            patch(
                "aegis.processing.workstream_detector.get_workstreams",
                new_callable=AsyncMock,
                return_value=[ws_mock],
            ),
            patch(
                "aegis.processing.workstream_detector.link_item_to_workstream",
                new_callable=AsyncMock,
            ) as mock_link,
        ):
            session = AsyncMock()
            result = await run_workstream_assignment(session)
            assert result["items_unassigned"] == 1
            mock_link.assert_not_called()


# ── Lifecycle Management Tests ─────────────────────────────


class TestWorkstreamLifecycle:
    @pytest.mark.asyncio
    async def test_auto_quiet_inactive_workstream(self):
        """Workstreams with no activity past quiet_days get marked quiet."""
        now = datetime.now(timezone.utc)

        # Mock workstream: active, 20 days since creation, no items
        ws_mock = MagicMock()
        ws_mock.id = 1
        ws_mock.name = "Old WS"
        ws_mock.status = "active"
        ws_mock.auto_quiet_days = 14
        ws_mock.created = now - timedelta(days=20)
        ws_mock.updated = now - timedelta(days=20)

        mock_session = AsyncMock()

        # func.max query returns None (no items)
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=scalar_result)

        with (
            patch(
                "aegis.processing.workstream_detector.get_workstreams",
                new_callable=AsyncMock,
                side_effect=lambda session, status_filter=None: (
                    [ws_mock] if status_filter == "active" else []
                ),
            ),
            patch(
                "aegis.processing.workstream_detector.update_workstream",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            result = await manage_workstream_lifecycle(mock_session)
            assert result["quieted"] == 1
            mock_update.assert_any_call(mock_session, 1, status="quiet")

    @pytest.mark.asyncio
    async def test_recently_active_workstream_stays_active(self):
        """Workstreams with recent activity stay active."""
        now = datetime.now(timezone.utc)

        ws_mock = MagicMock()
        ws_mock.id = 1
        ws_mock.name = "Active WS"
        ws_mock.status = "active"
        ws_mock.auto_quiet_days = 14
        ws_mock.created = now - timedelta(days=5)

        mock_session = AsyncMock()

        # func.max returns recent date
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none.return_value = now - timedelta(days=2)
        mock_session.execute = AsyncMock(return_value=scalar_result)

        with (
            patch(
                "aegis.processing.workstream_detector.get_workstreams",
                new_callable=AsyncMock,
                side_effect=lambda session, status_filter=None: (
                    [ws_mock] if status_filter == "active" else []
                ),
            ),
            patch(
                "aegis.processing.workstream_detector.update_workstream",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            result = await manage_workstream_lifecycle(mock_session)
            assert result["quieted"] == 0
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_archive_quiet_workstream(self):
        """Quiet workstreams after 90 days get archived."""
        now = datetime.now(timezone.utc)

        ws_quiet = MagicMock()
        ws_quiet.id = 2
        ws_quiet.name = "Quiet WS"
        ws_quiet.status = "quiet"
        ws_quiet.auto_quiet_days = 14
        ws_quiet.created = now - timedelta(days=180)
        ws_quiet.updated = now - timedelta(days=100)

        mock_session = AsyncMock()

        # No active workstreams, one quiet workstream
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=scalar_result)

        with (
            patch(
                "aegis.processing.workstream_detector.get_workstreams",
                new_callable=AsyncMock,
                side_effect=lambda session, status_filter=None: (
                    [ws_quiet] if status_filter == "quiet"
                    else [] if status_filter == "active"
                    else []
                ),
            ),
            patch(
                "aegis.processing.workstream_detector.update_workstream",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            result = await manage_workstream_lifecycle(mock_session)
            assert result["archived"] == 1
            mock_update.assert_any_call(mock_session, 2, status="archived")


# ── Layer 1 Naming Mock Test ──────────────────────────────


class TestWorkstreamNaming:
    @pytest.mark.asyncio
    async def test_name_workstream_via_llm(self):
        """LLM naming returns a valid name and description."""
        from aegis.processing.workstream_detector import _name_workstream_via_llm

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"name": "Q2 Budget Review", "description": "Budget review discussions for Q2 planning"}')]
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50

        with patch("aegis.processing.workstream_detector.AsyncAnthropic") as mock_cls:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            result = await _name_workstream_via_llm(["Budget meeting notes", "Q2 financial review", "Planning session"])
            assert result["name"] == "Q2 Budget Review"
            assert "Budget" in result["description"] or "Q2" in result["description"]

    @pytest.mark.asyncio
    async def test_name_workstream_llm_failure(self):
        """LLM failure returns fallback name."""
        from aegis.processing.workstream_detector import _name_workstream_via_llm

        with patch("aegis.processing.workstream_detector.AsyncAnthropic") as mock_cls:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))
            mock_cls.return_value = mock_client

            result = await _name_workstream_via_llm(["test"])
            assert result["name"] == "Unnamed Workstream"
