"""Tests for ``helios.capture.orchestrator.CaptureOrchestrator`` (Track 1F.2).

End-to-end with replay sources, plus dedicated tests for crash recovery,
single-active-session enforcement, calendar linking, and the on_stall →
unavailable-chunk path.

VirtualClock drives all timing so the chunk-boundary crossing test is
deterministic. A small ``_StubPool`` adapts the per-test ``aiosqlite``
connection to the orchestrator's ``DatabasePool`` interface — the real
pool is wired in production via the FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import numpy as np
import pytest

from helios.capture.orchestrator import CaptureOrchestrator
from helios.config import HeliosConfig
from helios.db import queries
from helios.db.migrations import run_migrations
from helios.sources import ReplaySourceFactory
from helios.sources.interface import (
    AudioSample,
    AudioSource,
    CalendarEvent,
    SystemAudioSource,
)
from tests.conftest import synth_wav


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubPool:
    """Minimal duck-type of ``DatabasePool`` for tests.

    The orchestrator only ever reads ``.writer``; tests don't exercise
    the reader path. Wrapping the test's aiosqlite connection avoids
    standing up a real pool (and the DB it would create on disk).
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    @property
    def writer(self):
        return self._conn


async def _settle(n: int = 8) -> None:
    """Yield to let newly-spawned tasks reach their first await."""
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.fixture
async def migrated_db(tmp_db, migrations_dir):
    await run_migrations(tmp_db, migrations_dir)
    return tmp_db


@pytest.fixture
def helios_config(tmp_path) -> HeliosConfig:
    cfg = HeliosConfig()
    cfg.storage.root = str(tmp_path / "capture")
    return cfg


@pytest.fixture
def replay_factory(tmp_path) -> ReplaySourceFactory:
    """A replay factory pointing at freshly-synthesised fixture files.

    The fixtures live under ``tmp_path`` so each test gets its own clean
    set; we make them long enough that a 30s chunk fits comfortably.
    """
    mic_wav = synth_wav(tmp_path / "fixtures" / "mic.wav", duration_s=40.0, freq_hz=440.0)
    sys_wav = synth_wav(tmp_path / "fixtures" / "sys.wav", duration_s=40.0, freq_hz=880.0)
    cal_json = tmp_path / "fixtures" / "calendar.json"
    cal_json.write_text("[]")
    # Speed multiplier doesn't matter under VirtualClock; sleeps only
    # complete on advance().
    return ReplaySourceFactory(mic_wav, sys_wav, cal_json, speed_multiplier=10.0)


@pytest.fixture
def orchestrator(
    migrated_db, helios_config, virtual_clock, replay_factory
) -> CaptureOrchestrator:
    return CaptureOrchestrator(
        db_pool=_StubPool(migrated_db),
        config=helios_config,
        clock=virtual_clock,
        source_factory=replay_factory,
    )


# ---------------------------------------------------------------------------
# 1. End-to-end with replay sources.
# ---------------------------------------------------------------------------


async def test_end_to_end_replay_produces_chunks_and_session_row(
    orchestrator, migrated_db, virtual_clock, helios_config
):
    """Start, drive past one chunk boundary, stop. Verify rows + WAVs."""
    session_id = await orchestrator.start_session(kind="continuous")
    assert orchestrator.active_session_id == session_id

    # Advance virtual time past the 30s chunk boundary so a full chunk
    # flushes for both channels. _settle gives reader/sleep tasks a
    # chance to register before we advance.
    await _settle()
    await virtual_clock.advance(35.0)
    await _settle()

    await orchestrator.stop_session(session_id, reason="manual")
    assert orchestrator.active_session_id is None

    # Session row exists, ended_at populated, end_reason recorded.
    cursor = await migrated_db.execute(
        "SELECT id, kind, ended_at, end_reason FROM capture_sessions WHERE id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    sid, kind, ended_at, end_reason = row
    assert sid == session_id
    assert kind == "continuous"
    assert ended_at is not None
    assert end_reason == "manual"

    # Audio chunks exist for both channels and the WAVs are on disk.
    chunks = await queries.get_session_audio_chunks(migrated_db, session_id)
    assert chunks, "expected at least one chunk row"
    channels_seen = {c.channel for c in chunks}
    assert channels_seen == {"mic", "system"}, f"got {channels_seen}"

    # At least one full chunk per channel must have been written to disk.
    full_chunks = [
        c for c in chunks if not c.partial and c.status == "recorded"
    ]
    assert full_chunks, "expected at least one full recorded chunk"
    for chunk in full_chunks:
        assert chunk.path is not None
        assert Path(chunk.path).exists(), f"missing WAV: {chunk.path}"


# ---------------------------------------------------------------------------
# 2. stop_session sets ended_at + flushes partial chunks.
# ---------------------------------------------------------------------------


async def test_stop_session_flushes_partial_chunks(
    migrated_db, helios_config, virtual_clock, tmp_path
):
    """Stop before a 30s chunk boundary → one partial chunk per channel.

    Uses a short (5s) WAV per channel so the entire fixture plays in
    well under the 30s chunk window — the leftover lands as a partial.
    """
    mic_wav = synth_wav(tmp_path / "fx_short" / "mic.wav", duration_s=5.0, freq_hz=440.0)
    sys_wav = synth_wav(tmp_path / "fx_short" / "sys.wav", duration_s=5.0, freq_hz=880.0)
    cal_json = tmp_path / "fx_short" / "cal.json"
    cal_json.write_text("[]")
    factory = ReplaySourceFactory(mic_wav, sys_wav, cal_json, speed_multiplier=10.0)

    orch = CaptureOrchestrator(
        db_pool=_StubPool(migrated_db),
        config=helios_config,
        clock=virtual_clock,
        source_factory=factory,
    )

    session_id = await orch.start_session(kind="continuous")

    # 5s of audio replayed at 10x = 0.5s of virtual time, plus a margin
    # so EOF latches before stop. No full chunk can flush (5s < 30s).
    await _settle()
    await virtual_clock.advance(1.0)
    await _settle(n=32)

    await orch.stop_session(session_id, reason="early_stop")

    chunks = await queries.get_session_audio_chunks(migrated_db, session_id)
    # Both channels should have flushed exactly one partial chunk.
    by_channel: dict[str, list] = {}
    for c in chunks:
        by_channel.setdefault(c.channel, []).append(c)
    assert set(by_channel) == {"mic", "system"}
    for ch, rows in by_channel.items():
        assert len(rows) == 1, f"{ch} should have exactly 1 partial row"
        assert rows[0].partial is True

    # ended_at and end_reason are persisted.
    cursor = await migrated_db.execute(
        "SELECT ended_at, end_reason FROM capture_sessions WHERE id = ?",
        (session_id,),
    )
    ended_at, end_reason = await cursor.fetchone()
    assert ended_at is not None
    assert end_reason == "early_stop"


# ---------------------------------------------------------------------------
# 3. Calendar events are linked to the session.
# ---------------------------------------------------------------------------


async def test_calendar_events_linked_on_start(
    orchestrator, migrated_db, virtual_clock
):
    """start_session(calendar_events=...) writes session_calendar_links rows."""
    now = virtual_clock.time()
    events = [
        CalendarEvent(
            id="cal-evt-1",
            title="Standup",
            start_ts=now,
            end_ts=now + 1800.0,
            attendees=["a@example.com", "b@example.com"],
        ),
        CalendarEvent(
            id="cal-evt-2",
            title="1:1",
            start_ts=now + 3600.0,
            end_ts=now + 5400.0,
            attendees=["c@example.com"],
        ),
    ]

    session_id = await orchestrator.start_session(
        kind="calendar", calendar_events=events
    )

    # Verify both links exist for this session_id with the correct
    # overlap window (the event's start_ts/end_ts at session-start time).
    cursor = await migrated_db.execute(
        "SELECT calendar_event_id, overlap_start, overlap_end "
        "FROM session_calendar_links "
        "WHERE session_id = ? ORDER BY calendar_event_id",
        (session_id,),
    )
    rows = await cursor.fetchall()
    assert [r[0] for r in rows] == ["cal-evt-1", "cal-evt-2"]
    # overlap_start/end must be the event window, not zero-duration.
    assert rows[0][1] == events[0].start_ts
    assert rows[0][2] == events[0].end_ts
    assert rows[1][1] == events[1].start_ts
    assert rows[1][2] == events[1].end_ts
    assert rows[0][2] > rows[0][1]  # non-zero duration

    # Bidirectional helper agrees.
    sessions_for_evt1 = await queries.get_sessions_for_calendar_event(
        migrated_db, "cal-evt-1"
    )
    assert any(s.id == session_id for s in sessions_for_evt1)

    await orchestrator.stop_session(session_id, reason="cleanup")


# ---------------------------------------------------------------------------
# 4. Crash recovery marks orphaned sessions ended.
# ---------------------------------------------------------------------------


async def test_recover_marks_orphan_sessions_ended(
    migrated_db, helios_config, virtual_clock, replay_factory
):
    """Sessions with ended_at IS NULL get end_reason='crash_recovery'."""
    # Insert a couple of orphan sessions directly via the helper.
    # (No SQL outside queries.py; this is the test plumbing too.)
    orphan1 = await queries.create_session(
        migrated_db, kind="continuous", started_at=1712340000.0
    )
    orphan2 = await queries.create_session(
        migrated_db, kind="manual_screen", started_at=1712341000.0
    )

    # Sanity: the active-session helper sees one of them.
    active = await queries.get_active_session(migrated_db)
    assert active is not None and active.id in (orphan1, orphan2)

    orch = CaptureOrchestrator(
        db_pool=_StubPool(migrated_db),
        config=helios_config,
        clock=virtual_clock,
        source_factory=replay_factory,
    )
    await orch.recover()

    # Both sessions are now closed with the recovery reason.
    cursor = await migrated_db.execute(
        "SELECT id, ended_at, end_reason FROM capture_sessions ORDER BY id"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 2
    for sid, ended_at, end_reason in rows:
        assert ended_at is not None, f"session {sid} not ended"
        assert end_reason == "crash_recovery", f"session {sid}: {end_reason}"

    # And no session is active anymore.
    assert await queries.get_active_session(migrated_db) is None


async def test_recover_with_no_orphans_is_noop(
    migrated_db, helios_config, virtual_clock, replay_factory
):
    """recover() on a clean DB doesn't raise and changes nothing."""
    orch = CaptureOrchestrator(
        db_pool=_StubPool(migrated_db),
        config=helios_config,
        clock=virtual_clock,
        source_factory=replay_factory,
    )
    await orch.recover()
    cursor = await migrated_db.execute("SELECT COUNT(*) FROM capture_sessions")
    (count,) = await cursor.fetchone()
    assert count == 0


# ---------------------------------------------------------------------------
# 5. Concurrent start raises.
# ---------------------------------------------------------------------------


async def test_concurrent_start_raises_runtime_error(
    orchestrator, virtual_clock
):
    """Starting a second session while one is active is rejected."""
    sid1 = await orchestrator.start_session(kind="continuous")
    try:
        with pytest.raises(RuntimeError, match="already active"):
            await orchestrator.start_session(kind="continuous")
    finally:
        await orchestrator.stop_session(sid1, reason="cleanup")


# ---------------------------------------------------------------------------
# 6. Stop with wrong session_id raises.
# ---------------------------------------------------------------------------


async def test_stop_with_wrong_session_id_raises_value_error(
    orchestrator, virtual_clock
):
    """stop_session(some_other_id) on the active session is rejected."""
    sid = await orchestrator.start_session(kind="continuous")
    try:
        with pytest.raises(ValueError, match="mismatch"):
            await orchestrator.stop_session(sid + 999, reason="bad_id")
        # Active session is still intact afterwards.
        assert orchestrator.active_session_id == sid
    finally:
        await orchestrator.stop_session(sid, reason="cleanup")


async def test_stop_with_no_active_session_raises_value_error(orchestrator):
    """stop_session called when nothing is running is a ValueError."""
    with pytest.raises(ValueError, match="no active"):
        await orchestrator.stop_session(1, reason="nope")


# ---------------------------------------------------------------------------
# Wave 5b: voice_note kind skips the system-audio source entirely so SCK
# never initializes (and thus never probes Screen Recording permission).
# ---------------------------------------------------------------------------


async def test_voice_note_kind_skips_system_source_construction(
    migrated_db, helios_config, virtual_clock, replay_factory
):
    """``start_session(kind="voice_note")`` must NOT create a system source.

    The factory's ``make_system_source`` is what triggers SCK init in the
    real backend. For voice notes we want the orchestrator to bypass that
    entirely so macOS never shows a Screen Recording permission prompt.
    """
    calls = {"mic": 0, "system": 0}

    class _CountingFactory:
        """Wraps a ReplaySourceFactory and counts make_*_source calls."""

        def __init__(self, inner):
            self._inner = inner

        def make_mic_source(self, *args, **kwargs):
            calls["mic"] += 1
            return self._inner.make_mic_source(*args, **kwargs)

        def make_system_source(self, *args, **kwargs):
            calls["system"] += 1
            return self._inner.make_system_source(*args, **kwargs)

        def make_calendar_source(self, *args, **kwargs):
            return self._inner.make_calendar_source(*args, **kwargs)

    factory = _CountingFactory(replay_factory)
    orch = CaptureOrchestrator(
        db_pool=_StubPool(migrated_db),
        config=helios_config,
        clock=virtual_clock,
        source_factory=factory,
    )

    sid = await orch.start_session(kind="voice_note")
    try:
        assert calls["mic"] == 1, "mic source should be created"
        assert calls["system"] == 0, (
            "voice_note must not call make_system_source — would trigger "
            "SCK and the macOS Screen Recording prompt"
        )
    finally:
        await orch.stop_session(sid, reason="cleanup")


async def test_continuous_kind_still_creates_system_source(
    migrated_db, helios_config, virtual_clock, replay_factory
):
    """Sanity: non-voice-note kinds still get a system source."""
    calls = {"system": 0}

    class _CountingFactory:
        def __init__(self, inner):
            self._inner = inner

        def make_mic_source(self, *args, **kwargs):
            return self._inner.make_mic_source(*args, **kwargs)

        def make_system_source(self, *args, **kwargs):
            calls["system"] += 1
            return self._inner.make_system_source(*args, **kwargs)

        def make_calendar_source(self, *args, **kwargs):
            return self._inner.make_calendar_source(*args, **kwargs)

    factory = _CountingFactory(replay_factory)
    orch = CaptureOrchestrator(
        db_pool=_StubPool(migrated_db),
        config=helios_config,
        clock=virtual_clock,
        source_factory=factory,
    )

    sid = await orch.start_session(kind="continuous")
    try:
        assert calls["system"] == 1
    finally:
        await orch.stop_session(sid, reason="cleanup")


# ---------------------------------------------------------------------------
# 7. on_stall callback writes an unavailable chunk row.
# ---------------------------------------------------------------------------


class _StallingSource:
    """Single-channel source that emits one block then blocks forever.

    Models a hung audio device: the watchdog should detect that no
    samples have arrived for ``watchdog_seconds`` and fire ``on_stall``.
    """

    def __init__(self, channel: str, clock) -> None:
        self._channel = channel
        self._clock = clock
        self._stop_event: asyncio.Event | None = None
        self._emitted = False
        self.is_running = False
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self._stop_event = asyncio.Event()
        self._emitted = False
        self.is_running = True

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        self.is_running = False

    async def samples(self) -> AsyncIterator[AudioSample]:
        assert self._stop_event is not None
        if not self._emitted:
            self._emitted = True
            yield AudioSample(
                channel=self._channel,  # type: ignore[arg-type]
                ts=self._clock.time(),
                samples=np.zeros(1600, dtype=np.int16),
            )
        await self._stop_event.wait()

    def video_frames(self):  # pragma: no cover - never called
        raise NotImplementedError


class _StallingSourceFactory:
    """SourceFactory that hands out _StallingSource instances on demand."""

    def __init__(self) -> None:
        self.mic_source: _StallingSource | None = None
        self.system_source: _StallingSource | None = None

    def make_mic_source(
        self,
        queue: asyncio.Queue[AudioSample],
        clock,
        start_ts: float | None = None,
    ) -> AudioSource:
        del queue, start_ts
        self.mic_source = _StallingSource("mic", clock)
        return self.mic_source  # type: ignore[return-value]

    def make_system_source(
        self,
        queue: asyncio.Queue[AudioSample],
        clock,
        start_ts: float | None = None,
    ) -> SystemAudioSource:
        del queue, start_ts
        self.system_source = _StallingSource("system", clock)
        return self.system_source  # type: ignore[return-value]

    def make_calendar_source(self, clock):  # pragma: no cover - unused
        raise NotImplementedError


async def test_on_stall_writes_unavailable_chunk_row(
    migrated_db, helios_config, virtual_clock
):
    """When the watchdog detects a stall, an unavailable chunk row appears."""
    factory = _StallingSourceFactory()
    orch = CaptureOrchestrator(
        db_pool=_StubPool(migrated_db),
        config=helios_config,
        clock=virtual_clock,
        source_factory=factory,
    )

    session_id = await orch.start_session(kind="continuous")
    # Let the readers consume their one block + register the wait.
    await _settle()

    # Default watchdog is 30s; advance 60s to give the stall multiple
    # chances to fire and avoid edge-of-window timing.
    await virtual_clock.advance(60.0)
    # Drain heavily — _check_for_stalls processes channels sequentially
    # and each on_stall→insert→restart yields several times.
    await _settle(n=64)

    # Stop FIRST so any in-flight watchdog work completes / cancels
    # cleanly before we inspect the DB. Stopping doesn't roll back the
    # already-recorded unavailable rows.
    await orch.stop_session(session_id, reason="cleanup")

    # Find the unavailable chunk rows directly via the queries helper.
    chunks = await queries.get_session_audio_chunks(migrated_db, session_id)
    unavailable = [c for c in chunks if c.status == "unavailable"]
    assert unavailable, (
        f"expected at least one unavailable chunk, got "
        f"{[(c.channel, c.status) for c in chunks]}"
    )
    # Both channels stalled, so we should see at least one row per channel.
    channels = {c.channel for c in unavailable}
    assert "mic" in channels
    assert "system" in channels
    # And the reason is wired through.
    for c in unavailable:
        assert c.unavailable_reason == "watchdog_stall"


# ---------------------------------------------------------------------------
# 8. Stream startup failure cleans up the session row.
# ---------------------------------------------------------------------------


class _BadFactory:
    """Factory whose mic source raises on start — to exercise rollback."""

    def make_mic_source(self, queue, clock, start_ts=None):
        del queue, clock, start_ts

        class _Bad:
            is_running = False

            async def start(self) -> None:
                raise RuntimeError("device offline")

            async def stop(self) -> None:
                return None

            async def samples(self):
                if False:  # pragma: no cover
                    yield

            def video_frames(self):  # pragma: no cover
                raise NotImplementedError

        return _Bad()

    def make_system_source(self, queue, clock, start_ts=None):
        # Won't be reached because mic fails first.
        return self.make_mic_source(queue, clock, start_ts)

    def make_calendar_source(self, clock):  # pragma: no cover - unused
        raise NotImplementedError


async def test_stream_start_failure_ends_session_and_clears_state(
    migrated_db, helios_config, virtual_clock
):
    """If stream startup raises, the session row is closed + state cleared."""
    orch = CaptureOrchestrator(
        db_pool=_StubPool(migrated_db),
        config=helios_config,
        clock=virtual_clock,
        source_factory=_BadFactory(),
    )

    with pytest.raises(Exception):
        await orch.start_session(kind="continuous")

    # No active session is left over.
    assert orch.active_session_id is None

    # The session row has end_reason='stream_start_failed'.
    cursor = await migrated_db.execute(
        "SELECT id, ended_at, end_reason FROM capture_sessions ORDER BY id"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    _sid, ended_at, end_reason = rows[0]
    assert ended_at is not None
    assert end_reason == "stream_start_failed"

    # And we can start a fresh session right after the failure.
    # (Use a working factory for this part.)
    mic_wav = synth_wav(Path(helios_config.storage.root) / "fx" / "m.wav", duration_s=2.0)
    sys_wav = synth_wav(Path(helios_config.storage.root) / "fx" / "s.wav", duration_s=2.0)
    cal_json = Path(helios_config.storage.root) / "fx" / "c.json"
    cal_json.write_text("[]")
    orch._factory = ReplaySourceFactory(mic_wav, sys_wav, cal_json, speed_multiplier=10.0)
    sid = await orch.start_session(kind="continuous")
    await orch.stop_session(sid, reason="cleanup")
