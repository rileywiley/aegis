"""Unit tests for the 60s self-test orchestrator.

Exercises :class:`helios.workers.self_test.SelfTestRunner` against
mocked workers + an in-memory aiosqlite DB so we never touch real
audio or run the live capture pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from helios.db import queries
from helios.workers.self_test import SelfTestRunner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def schema_db(tmp_path):
    """Schema-loaded SQLite DB matching the production migration."""
    db_path = tmp_path / "test.db"
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA foreign_keys = ON")
    migration = Path(__file__).parent.parent / "migrations" / "001_initial.sql"
    sql = migration.read_text()
    await db.executescript(sql)
    await db.commit()
    yield db
    await db.close()


@dataclass
class _FakeTxResult:
    segments: list[Any]


class _FakeOrchestrator:
    """Minimal orchestrator stub: tracks the active session id only."""

    def __init__(self, db) -> None:
        self._db = db
        self.active_session_id: int | None = None
        self.start_calls = 0
        self.stop_calls = 0

    async def start_session(self, kind: str) -> int:
        self.start_calls += 1
        sid = await queries.create_session(self._db, kind)
        # Seed one mic chunk per session start so verify_chunks /
        # transcribe steps have something to look at.
        await queries.insert_audio_chunk(
            self._db,
            session_id=sid,
            channel="mic",
            start_ts=0.0,
            end_ts=30.0,
            path=f"/tmp/fake_s{sid}.wav",
            samples=16000 * 30,
        )
        self.active_session_id = sid
        return sid

    async def stop_session(self, session_id: int, reason: str) -> None:
        self.stop_calls += 1
        await queries.update_session_ended(
            self._db, session_id=session_id, ended_at=60.0, end_reason=reason
        )
        if self.active_session_id == session_id:
            self.active_session_id = None


def _make_config(*, diarization_enabled: bool = False) -> Any:
    class _Cfg:
        class diarization:  # noqa: N801 - mirror real attribute access
            enabled = diarization_enabled

    return _Cfg()


def _make_transcription_worker(segments: int = 2) -> Any:
    worker = MagicMock()
    fake = _FakeTxResult(segments=[object() for _ in range(segments)])
    worker.transcribe_synchronously = AsyncMock(return_value=fake)
    return worker


def _make_diarization_worker() -> Any:
    worker = MagicMock()
    worker.enqueue_session = AsyncMock(return_value=None)
    return worker


async def _no_sleep(_seconds: float) -> None:
    """Replacement for asyncio.sleep — fast-forward."""
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_self_test_happy_path_complete(schema_db, tmp_path):
    orch = _FakeOrchestrator(schema_db)
    tx = _make_transcription_worker(segments=3)
    runner = SelfTestRunner(
        db=schema_db,
        orchestrator=orch,
        config=_make_config(diarization_enabled=False),
        storage_root=tmp_path,
        transcription_worker=tx,
        diarization_worker=None,
        capture_seconds=60.0,
        sleep=_no_sleep,
    )
    result = await runner.run()
    assert result.status == "complete"
    assert result.session_id is not None
    step_names = [s.name for s in result.steps]
    assert step_names == [
        "start_session",
        "capture_window",
        "stop_session",
        "verify_chunks",
        "transcribe",
        "diarize",
    ]
    assert all(s.ok for s in result.steps)
    # Cleanup deleted the row.
    row = await queries.get_session_by_id(schema_db, result.session_id)
    assert row is None


async def test_self_test_session_start_fails(schema_db, tmp_path):
    orch = _FakeOrchestrator(schema_db)
    orch.start_session = AsyncMock(side_effect=RuntimeError("nope"))
    runner = SelfTestRunner(
        db=schema_db,
        orchestrator=orch,
        config=_make_config(),
        storage_root=tmp_path,
        transcription_worker=_make_transcription_worker(),
        diarization_worker=None,
        sleep=_no_sleep,
    )
    result = await runner.run()
    assert result.status == "failed"
    assert result.steps[0].name == "start_session"
    assert result.steps[0].ok is False
    # No session was opened so cleanup is a no-op.
    assert result.session_id is None


async def test_self_test_transcription_failure_marks_step(schema_db, tmp_path):
    orch = _FakeOrchestrator(schema_db)
    tx = MagicMock()
    tx.transcribe_synchronously = AsyncMock(side_effect=RuntimeError("boom"))
    runner = SelfTestRunner(
        db=schema_db,
        orchestrator=orch,
        config=_make_config(),
        storage_root=tmp_path,
        transcription_worker=tx,
        diarization_worker=None,
        sleep=_no_sleep,
    )
    result = await runner.run()
    assert result.status == "failed"
    tx_step = next(s for s in result.steps if s.name == "transcribe")
    assert tx_step.ok is False
    assert "RuntimeError" in tx_step.detail
    # Session row should still be deleted by cleanup.
    row = await queries.get_session_by_id(schema_db, result.session_id)
    assert row is None


async def test_self_test_diarize_skipped_when_disabled(schema_db, tmp_path):
    orch = _FakeOrchestrator(schema_db)
    runner = SelfTestRunner(
        db=schema_db,
        orchestrator=orch,
        config=_make_config(diarization_enabled=False),
        storage_root=tmp_path,
        transcription_worker=_make_transcription_worker(),
        diarization_worker=None,
        sleep=_no_sleep,
    )
    result = await runner.run()
    diar_step = next(s for s in result.steps if s.name == "diarize")
    assert diar_step.ok is True
    assert "skipped" in diar_step.detail.lower()


async def test_self_test_diarize_enqueues_when_enabled(schema_db, tmp_path):
    orch = _FakeOrchestrator(schema_db)
    diar = _make_diarization_worker()
    runner = SelfTestRunner(
        db=schema_db,
        orchestrator=orch,
        config=_make_config(diarization_enabled=True),
        storage_root=tmp_path,
        transcription_worker=_make_transcription_worker(),
        diarization_worker=diar,
        diarization_wait_seconds=0.5,
        sleep=_no_sleep,
    )
    result = await runner.run()
    diar.enqueue_session.assert_awaited()
    diar_step = next(s for s in result.steps if s.name == "diarize")
    # final status is "pending" because nothing flips it without the
    # real worker — the step reports ok=False but the test confirms
    # enqueue was called.
    assert "final status" in diar_step.detail


async def test_self_test_cleanup_runs_even_after_failure(schema_db, tmp_path):
    orch = _FakeOrchestrator(schema_db)
    tx = MagicMock()
    tx.transcribe_synchronously = AsyncMock(side_effect=RuntimeError("x"))
    runner = SelfTestRunner(
        db=schema_db,
        orchestrator=orch,
        config=_make_config(),
        storage_root=tmp_path,
        transcription_worker=tx,
        diarization_worker=None,
        sleep=_no_sleep,
    )
    result = await runner.run()
    assert result.session_id is not None
    # Row deleted regardless of step failures.
    assert await queries.get_session_by_id(schema_db, result.session_id) is None


async def test_self_test_verify_chunks_fails_when_no_audio(schema_db, tmp_path):
    """Force orchestrator to start a session with no chunks."""
    db = schema_db

    class _OrchNoChunks(_FakeOrchestrator):
        async def start_session(self, kind: str) -> int:
            self.start_calls += 1
            sid = await queries.create_session(self._db, kind)
            self.active_session_id = sid
            return sid  # no chunk inserted

    orch = _OrchNoChunks(db)
    runner = SelfTestRunner(
        db=db,
        orchestrator=orch,
        config=_make_config(),
        storage_root=tmp_path,
        transcription_worker=_make_transcription_worker(),
        diarization_worker=None,
        sleep=_no_sleep,
    )
    result = await runner.run()
    verify_step = next(s for s in result.steps if s.name == "verify_chunks")
    assert verify_step.ok is False
    assert "no chunks" in verify_step.detail.lower()
