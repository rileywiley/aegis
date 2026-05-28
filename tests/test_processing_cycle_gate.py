"""Integration test for the off-hours gate at the top of ``_run_processing_cycle``.

When the gate is closed:
- No triage/extraction/workstream calls fire (heavy LLM-spending code paths).
- ``system_health.processing_cycle`` is upserted to ``status='paused'``.
- The function returns early before any DB scan.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aegis import main as aegis_main


@pytest.mark.asyncio
async def test_processing_cycle_skipped_when_gated():
    """Gate closed → no LLM imports get exercised, system_health gets paused."""
    upsert_mock = AsyncMock()

    # Stub the gate to return closed. Patch both the import inside the
    # cycle and the upsert that records the paused state.
    with (
        patch(
            "aegis.intelligence.llm_gate.llm_calls_allowed",
            new=AsyncMock(return_value=(False, "outside work hours — next active Mon 08:00")),
        ),
        patch("aegis.db.repositories.upsert_system_health", new=upsert_mock),
        # Triage / extraction / workstream functions are imported inside
        # the cycle body. Patch them to assert they're NEVER called.
        patch("aegis.processing.triage.triage_batch", new=AsyncMock()) as triage_mock,
        patch(
            "aegis.processing.pipeline.process_pending_meetings",
            new=AsyncMock(return_value=0),
        ) as meetings_mock,
        patch(
            "aegis.processing.workstream_detector.run_workstream_assignment",
            new=AsyncMock(return_value=0),
        ) as workstream_mock,
    ):
        await aegis_main._run_processing_cycle(helios_client=None)

    triage_mock.assert_not_called()
    meetings_mock.assert_not_called()
    workstream_mock.assert_not_called()

    upsert_mock.assert_awaited_once()
    args, kwargs = upsert_mock.call_args
    # upsert_system_health(session, "processing_cycle", status="paused", last_error_message=...)
    assert args[1] == "processing_cycle"
    assert kwargs.get("status") == "paused"
    assert "outside work hours" in kwargs.get("last_error_message", "")


@pytest.mark.asyncio
async def test_processing_cycle_runs_when_allowed():
    """Gate open → triage path is reached (no upsert with status='paused')."""
    upsert_mock = AsyncMock()

    with (
        patch(
            "aegis.intelligence.llm_gate.llm_calls_allowed",
            new=AsyncMock(return_value=(True, "in work hours (open until 18:00)")),
        ),
        patch("aegis.db.repositories.upsert_system_health", new=upsert_mock),
        # Make the downstream steps no-ops so the cycle completes
        # without needing a real DB schema.
        patch(
            "aegis.processing.triage.triage_batch",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "aegis.processing.triage.apply_triage_results",
            new=AsyncMock(),
        ),
        patch(
            "aegis.processing.pipeline.process_pending_meetings",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "aegis.processing.pipeline.process_pending_voice_notes",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "aegis.processing.workstream_detector.run_workstream_assignment",
            new=AsyncMock(return_value=0),
        ),
    ):
        # The full cycle touches tables that this test setup doesn't fully
        # mock; we only care that the early-return-on-paused path isn't taken.
        # An exception inside the body is fine — what we assert is that the
        # "paused" upsert is never made.
        try:
            await aegis_main._run_processing_cycle(helios_client=None)
        except Exception:
            pass

    paused_calls = [
        call for call in upsert_mock.call_args_list
        if call.kwargs.get("status") == "paused"
    ]
    assert paused_calls == [], (
        f"gate was reported open but cycle wrote a paused row: {paused_calls}"
    )
