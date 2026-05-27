"""Tests for helios.components.ComponentStatusReporter (Wave 1A / Phase 3)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from helios.components import ComponentStatusReporter
from helios.db.migrations import run_migrations
from helios.db.queries import get_component_status
from helios.state import DaemonStateMachine


@pytest_asyncio.fixture
async def db(tmp_db, migrations_dir):
    """tmp_db with migrations applied."""
    await run_migrations(tmp_db, migrations_dir)
    return tmp_db


@pytest.fixture
def state_machine() -> DaemonStateMachine:
    return DaemonStateMachine()


@pytest_asyncio.fixture
async def reporter(db, state_machine) -> ComponentStatusReporter:
    return ComponentStatusReporter(db=db, state_machine=state_machine)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_set_persists_to_db(db, reporter):
    await reporter.set("transcription", "ok")
    row = await get_component_status(db, "transcription")
    assert row is not None
    assert row.component == "transcription"
    assert row.status == "ok"


async def test_set_persists_reason_detail_action(db, reporter):
    await reporter.set(
        "transcription",
        "unavailable",
        reason="model_not_downloaded",
        detail="distil-large-v3 missing",
        action="Open Settings → Transcription → Download model",
    )
    row = await get_component_status(db, "transcription")
    assert row is not None
    assert row.status == "unavailable"
    assert row.reason == "model_not_downloaded"
    assert row.detail == "distil-large-v3 missing"
    assert row.action == "Open Settings → Transcription → Download model"


async def test_set_idempotent_skips_duplicate(db, reporter):
    """Calling set() with the same status+reason twice writes only once."""
    await reporter.set("transcription", "ok", reason=None)
    # First call wrote one row. Capture its ts.
    row1 = await get_component_status(db, "transcription")
    ts_first = row1.ts

    # Second call with identical inputs should be a no-op.
    await reporter.set("transcription", "ok", reason=None)
    row2 = await get_component_status(db, "transcription")
    assert row2.ts == ts_first  # same row, not re-written

    # Cache only holds one record for the component.
    assert len(reporter.all()) == 1


async def test_set_changing_reason_writes_new_row(db, reporter):
    """A different reason (even at the same status) DOES re-persist."""
    await reporter.set("transcription", "degraded", reason="slow_disk")
    first = await get_component_status(db, "transcription")
    await reporter.set("transcription", "degraded", reason="cpu_overloaded")
    second = await get_component_status(db, "transcription")
    assert second.reason == "cpu_overloaded"
    # New row replaces old (upsert semantics): only one row in DB.
    cursor = await db.execute(
        "SELECT COUNT(*) FROM component_status WHERE component = ?",
        ("transcription",),
    )
    (n,) = await cursor.fetchone()
    assert n == 1
    # And reason changed.
    assert first.reason != second.reason


# ---------------------------------------------------------------------------
# State-machine wiring
# ---------------------------------------------------------------------------


async def test_status_error_fires_state_machine_transition(reporter, state_machine):
    await reporter.set(
        "transcription",
        "error",
        reason="model_load_failed",
        detail="cuda out of memory",
    )
    state = state_machine.current()
    assert "transcription" in state.component_errors
    err = state.component_errors["transcription"]
    assert err.component == "transcription"
    assert err.message == "model_load_failed"


async def test_error_without_reason_uses_fallback_message(reporter, state_machine):
    """An error with no reason still produces a ComponentError with a default message."""
    await reporter.set("ocr", "error")
    state = state_machine.current()
    assert "ocr" in state.component_errors
    assert state.component_errors["ocr"].message == "error"


async def test_recovery_clears_component_error(reporter, state_machine):
    """Going from error → ok removes ONLY that component's entry."""
    await reporter.set("transcription", "error", reason="boom")
    assert "transcription" in state_machine.current().component_errors

    await reporter.set("transcription", "ok")
    state_after = state_machine.current()
    assert "transcription" not in state_after.component_errors


async def test_independent_components(reporter, state_machine):
    """Multiple components in error are tracked separately; recovery is per-component."""
    await reporter.set("transcription", "error", reason="model_load_failed")
    await reporter.set("diarization", "error", reason="hf_token_missing")

    state = state_machine.current()
    assert "transcription" in state.component_errors
    assert "diarization" in state.component_errors

    # Recover transcription only — diarization error must remain.
    await reporter.set("transcription", "ok")
    state_after = state_machine.current()
    assert "transcription" not in state_after.component_errors
    assert "diarization" in state_after.component_errors
    assert state_after.component_errors["diarization"].message == "hf_token_missing"


async def test_unavailable_does_not_fire_error_transition(reporter, state_machine):
    """``unavailable`` is not an error — no component_errors entry should appear."""
    await reporter.set(
        "transcription",
        "unavailable",
        reason="model_not_downloaded",
    )
    assert "transcription" not in state_machine.current().component_errors


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


async def test_get_returns_record(reporter):
    assert reporter.get("missing") is None
    await reporter.set("transcription", "ok")
    rec = reporter.get("transcription")
    assert rec is not None
    assert rec.component == "transcription"
    assert rec.status == "ok"
    assert rec.updated_at > 0


async def test_all_returns_independent_copy(reporter):
    await reporter.set("transcription", "ok")
    snapshot = reporter.all()
    snapshot["bogus"] = "tampered"  # type: ignore[assignment]
    # Mutating the returned dict must not affect the reporter's internal cache.
    assert "bogus" not in reporter.all()
