"""Tests for triage layer."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.processing.triage import TriageResult, triage_batch


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.anthropic_api_key = "test-key"
    return s


@pytest.fixture
def sample_items():
    return [
        {"id": 1, "preview": "Please review the budget proposal and send feedback by Friday", "source_type": "email"},
        {"id": 2, "preview": "Sounds good, thanks!", "source_type": "email"},
        {"id": 3, "preview": "Your password has been reset", "source_type": "email"},
    ]


@pytest.fixture
def canned_response():
    return [
        {"item_id": 1, "triage_class": "substantive", "score": 0.9, "reason": "Contains an ask with deadline"},
        {"item_id": 2, "triage_class": "contextual", "score": 0.4, "reason": "Brief acknowledgment"},
        {"item_id": 3, "triage_class": "noise", "score": 0.1, "reason": "Automated system notification"},
    ]


async def test_triage_batch_returns_results(mock_settings, sample_items, canned_response):
    """Triage batch classifies items correctly."""
    import json

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=json.dumps(canned_response))]
    mock_message.usage = MagicMock(input_tokens=100, output_tokens=50)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    mock_session = AsyncMock()

    with (
        patch("aegis.processing.triage.get_settings", return_value=mock_settings),
        patch("aegis.processing.triage.AsyncAnthropic", return_value=mock_client),
    ):
        results = await triage_batch(mock_session, sample_items)

    assert len(results) == 3
    assert results[0].triage_class == "substantive"
    assert results[0].score == 0.9
    assert results[1].triage_class == "contextual"
    assert results[2].triage_class == "noise"


async def test_triage_batch_empty_input():
    """Empty items list returns empty results."""
    mock_session = AsyncMock()
    results = await triage_batch(mock_session, [])
    assert results == []


async def test_triage_batch_handles_code_block_response(mock_settings, sample_items, canned_response):
    """Triage handles response wrapped in markdown code blocks."""
    import json

    wrapped = f"```json\n{json.dumps(canned_response)}\n```"
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=wrapped)]
    mock_message.usage = MagicMock(input_tokens=100, output_tokens=50)

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    mock_session = AsyncMock()

    with (
        patch("aegis.processing.triage.get_settings", return_value=mock_settings),
        patch("aegis.processing.triage.AsyncAnthropic", return_value=mock_client),
    ):
        results = await triage_batch(mock_session, sample_items)

    assert len(results) == 3


async def test_triage_result_model():
    """TriageResult validates correctly."""
    r = TriageResult(item_id=1, triage_class="substantive", score=0.85, reason="Has action items")
    assert r.item_id == 1
    assert r.triage_class == "substantive"
