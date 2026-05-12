"""Tests for ``helios.workers.transcription`` — Wave 2E / Track 3B.

Strategy:

* Mock at the **import level** (``monkeypatch.setitem(sys.modules,
  "whisperx", FakeWhisperX)``) so the lazy import inside
  ``_load_model_background`` resolves to the fake.
* Use ``tmp_db`` + migrations so the queries hit a real schema.
* Drive the worker by calling ``start()`` and then awaiting until
  ``_model_ready`` fires (the load runs in an executor — we yield with
  ``asyncio.sleep(0)`` cycles until ready).
* Use ``virtual_clock`` for tests that need to advance the poll loop's
  sleep deterministically, but most tests just call ``_process_chunk``
  directly to keep things synchronous and fast.

The ``@pytest.mark.slow`` test exercises real WhisperX. It is skipped
unless the ``transcription`` extras are installed AND the test is
explicitly selected with ``-m slow``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import pytest_asyncio

from helios.clock import VirtualClock
from helios.components import ComponentStatusReporter
from helios.config import HeliosConfig
from helios.db import queries
from helios.db.migrations import run_migrations
from helios.state import DaemonStateMachine
from helios.workers.transcription import (
    TranscribedSegment,
    TranscriptionWorker,
    _load_wav_mono_16k,
)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeModel:
    """Records calls to ``transcribe`` and returns canned segments."""

    def __init__(self, segments: list[dict] | Exception | None = None) -> None:
        self._segments = segments if segments is not None else []
        self.calls: list[tuple[str, dict]] = []

    def transcribe(self, path: str, **kwargs: Any) -> dict:
        self.calls.append((path, kwargs))
        if isinstance(self._segments, Exception):
            raise self._segments
        return {"segments": list(self._segments)}


class _FakeWhisperX:
    """Stand-in for the ``whisperx`` package's ``load_model`` factory."""

    def __init__(
        self,
        model: _FakeModel | None = None,
        load_raises: Exception | None = None,
        load_delay: float = 0.0,
    ) -> None:
        self.model = model if model is not None else _FakeModel()
        self.load_raises = load_raises
        self.load_delay = load_delay
        self.load_calls: list[tuple[tuple, dict]] = []

    def load_model(self, *args: Any, **kwargs: Any):
        self.load_calls.append((args, kwargs))
        if self.load_delay > 0:
            import time as _time

            _time.sleep(self.load_delay)
        if self.load_raises is not None:
            raise self.load_raises
        return self.model


def _install_fake_whisperx(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    """Inject ``fake`` so ``import whisperx`` finds it inside the worker."""
    monkeypatch.setitem(sys.modules, "whisperx", fake)


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


@pytest.fixture
def cfg() -> HeliosConfig:
    return HeliosConfig()


def _make_worker(
    *,
    db,
    cfg: HeliosConfig,
    reporter: ComponentStatusReporter,
    poll_seconds: float = 0.05,
    clock=None,
) -> TranscriptionWorker:
    return TranscriptionWorker(
        db=db,
        config=cfg,
        clock=clock if clock is not None else VirtualClock(initial_ts=1_000.0),
        reporter=reporter,
        poll_seconds=poll_seconds,
    )


async def _wait_for(predicate, *, timeout: float = 1.0, step: float = 0.01) -> None:
    """Yield to the event loop until ``predicate()`` is truthy or timeout.

    ``predicate`` may be sync (returns bool) or async (returns awaitable bool).
    """
    elapsed = 0.0
    while elapsed < timeout:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(step)
        elapsed += step
    raise AssertionError("predicate never became true")


def _write_silent_wav(path: str, *, duration_seconds: float = 0.1) -> None:
    """Write a tiny 16 kHz mono 16-bit PCM WAV of silence.

    The fake model ignores audio content, but the worker now pre-decodes
    the file with stdlib ``wave``, so the path must point at a real WAV
    in the format the capture pipeline produces.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    samples = np.zeros(int(16000 * duration_seconds), dtype=np.int16)
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(samples.tobytes())


async def _make_session_with_chunk(
    db,
    *,
    channel: str = "mic",
    path: str = "/tmp/fake.wav",
    start_ts: float = 1_000.0,
) -> tuple[int, int]:
    _write_silent_wav(path)
    sid = await queries.create_session(db, "manual_screen", started_at=start_ts)
    cid = await queries.insert_audio_chunk(
        db,
        session_id=sid,
        channel=channel,
        start_ts=start_ts,
        end_ts=start_ts + 30.0,
        path=path,
        samples=16000 * 30,
    )
    return sid, cid


# ---------------------------------------------------------------------------
# Model load + status reporting
# ---------------------------------------------------------------------------


async def test_start_reports_model_loading_then_ok(db, cfg, reporter, monkeypatch):
    fake = _FakeWhisperX()
    _install_fake_whisperx(monkeypatch, fake)
    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        # The "model_loading" status was set synchronously inside start().
        # The OK transition fires from the executor thread once load_model
        # returns. Wait for the event.
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        rec = reporter.get("transcription")
        assert rec is not None
        assert rec.status == "ok"
        assert worker._model is fake.model
    finally:
        await worker.stop()


async def test_start_reports_unavailable_when_whisperx_missing(
    db, cfg, reporter, monkeypatch
):
    """Lazy ``import whisperx`` raises ImportError ⇒ unavailable, not error."""
    # Stuff ``None`` into ``sys.modules['whisperx']`` — Python's import
    # machinery treats this as a cached "module not importable" sentinel
    # and raises ImportError on ``import whisperx`` without trying to
    # resolve the package on disk.
    monkeypatch.setitem(sys.modules, "whisperx", None)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await _wait_for(
            lambda: (rec := reporter.get("transcription")) is not None
            and rec.reason == "whisperx_not_installed",
            timeout=2.0,
        )
        rec = reporter.get("transcription")
        assert rec is not None
        assert rec.status == "unavailable"
        assert rec.reason == "whisperx_not_installed"
        # Worker never finished loading.
        assert not worker._model_ready.is_set()
    finally:
        await worker.stop()


async def test_start_reports_error_on_model_load_exception(
    db, cfg, reporter, monkeypatch
):
    fake = _FakeWhisperX(load_raises=RuntimeError("model corrupt"))
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await _wait_for(
            lambda: (rec := reporter.get("transcription")) is not None
            and rec.status == "error",
            timeout=2.0,
        )
        rec = reporter.get("transcription")
        assert rec is not None
        assert rec.status == "error"
        assert rec.reason == "model_load_failed"
        assert "model corrupt" in (rec.detail or "")
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# Chunk processing — speaker assignment
# ---------------------------------------------------------------------------


async def test_mic_chunk_segments_get_speaker_user(db, cfg, reporter, monkeypatch):
    fake_model = _FakeModel(
        segments=[
            {"start": 0.0, "end": 1.0, "text": "hello"},
            {"start": 1.0, "end": 2.0, "text": "world"},
        ]
    )
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        sid, cid = await _make_session_with_chunk(db, channel="mic")
        chunk = await queries.get_chunk_by_id(db, cid)
        assert chunk is not None
        await worker._process_chunk(chunk)
        rows = await queries.get_segments_for_chunk(db, cid)
        assert len(rows) == 2
        for r in rows:
            assert r.speaker == "user"
        # And the chunk is marked transcribed.
        marked = await queries.get_chunk_by_id(db, cid)
        assert marked is not None
        assert marked.status == "transcribed"
        assert marked.transcribed_at is not None
    finally:
        await worker.stop()


async def test_system_chunk_segments_have_null_speaker(
    db, cfg, reporter, monkeypatch
):
    fake_model = _FakeModel(
        segments=[{"start": 0.0, "end": 0.5, "text": "static"}]
    )
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        sid, cid = await _make_session_with_chunk(db, channel="system")
        chunk = await queries.get_chunk_by_id(db, cid)
        assert chunk is not None
        await worker._process_chunk(chunk)
        rows = await queries.get_segments_for_chunk(db, cid)
        assert len(rows) == 1
        assert rows[0].speaker is None
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# Periodic loop processes pending chunks
# ---------------------------------------------------------------------------


async def test_loop_drains_pending_chunks(db, cfg, reporter, monkeypatch):
    fake_model = _FakeModel(
        segments=[{"start": 0.0, "end": 1.0, "text": "ok"}]
    )
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    # Real asyncio sleep so the loop actually runs; small poll interval.
    from helios.clock import RealClock

    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter, poll_seconds=0.01,
        clock=RealClock(),
    )

    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    chunk_ids: list[int] = []
    for i in range(3):
        wav_path = f"/tmp/c{i}.wav"
        _write_silent_wav(wav_path)
        cid = await queries.insert_audio_chunk(
            db,
            session_id=sid,
            channel="mic",
            start_ts=1_000.0 + i * 30,
            end_ts=1_030.0 + i * 30,
            path=wav_path,
            samples=16000 * 30,
        )
        chunk_ids.append(cid)

    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        # Wait for all three chunks to be marked transcribed.
        async def _all_done() -> bool:
            for cid in chunk_ids:
                row = await queries.get_chunk_by_id(db, cid)
                if row is None or row.status != "transcribed":
                    return False
            return True

        await _wait_for(_all_done, timeout=2.0)
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# Retry / failure semantics
# ---------------------------------------------------------------------------


async def test_chunk_failing_three_times_marked_failed(
    db, cfg, reporter, monkeypatch
):
    cfg.transcription.transcription_max_attempts = 3
    fake_model = _FakeModel(segments=RuntimeError("decode error"))
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        sid, cid = await _make_session_with_chunk(db, channel="mic")
        # Three attempts.
        for attempt_no in range(3):
            chunk = await queries.get_chunk_by_id(db, cid)
            assert chunk is not None
            await worker._process_chunk(chunk)
        final = await queries.get_chunk_by_id(db, cid)
        assert final is not None
        assert final.status == "transcription_failed"
        assert final.transcription_attempts == 3
    finally:
        await worker.stop()


async def test_chunk_failing_once_is_retried(db, cfg, reporter, monkeypatch):
    cfg.transcription.transcription_max_attempts = 3
    fake_model = _FakeModel(segments=RuntimeError("transient"))
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        sid, cid = await _make_session_with_chunk(db, channel="mic")
        chunk = await queries.get_chunk_by_id(db, cid)
        assert chunk is not None
        await worker._process_chunk(chunk)
        # After one failure: status unchanged, attempts bumped.
        row = await queries.get_chunk_by_id(db, cid)
        assert row is not None
        assert row.status == "recorded"
        assert row.transcription_attempts == 1
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# transcribe_synchronously — voice-note path
# ---------------------------------------------------------------------------


async def test_transcribe_synchronously_returns_segments(
    db, cfg, reporter, monkeypatch
):
    fake_model = _FakeModel(
        segments=[
            {"start": 0.0, "end": 1.0, "text": "first"},
            {"start": 1.0, "end": 2.0, "text": "second"},
        ]
    )
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        sid, cid = await _make_session_with_chunk(db, channel="mic")
        chunk = await queries.get_chunk_by_id(db, cid)
        assert chunk is not None
        result = await worker.transcribe_synchronously([chunk])
        assert len(result.segments) == 2
        # And we wrote rows to the DB so subsequent reads see them.
        rows = await queries.get_segments_for_chunk(db, cid)
        assert len(rows) == 2
        # Chunk marked transcribed.
        marked = await queries.get_chunk_by_id(db, cid)
        assert marked is not None
        assert marked.status == "transcribed"
    finally:
        await worker.stop()


async def test_transcribe_synchronously_reuses_existing_segments(
    db, cfg, reporter, monkeypatch
):
    """Already-transcribed chunks return their existing rows — no re-call."""
    fake_model = _FakeModel(
        segments=[{"start": 0.0, "end": 1.0, "text": "should not run"}]
    )
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        sid, cid = await _make_session_with_chunk(db, channel="mic")
        # Pre-populate a segment + mark transcribed (simulates the periodic
        # worker having already consumed this chunk).
        await queries.insert_transcript_segment(
            db,
            chunk_id=cid,
            segment_index=0,
            start_ts=1_000.0,
            end_ts=1_002.0,
            text="prior segment",
            speaker="user",
            words=None,
        )
        await queries.mark_chunk_transcribed(db, cid)

        chunk = await queries.get_chunk_by_id(db, cid)
        assert chunk is not None
        # Reset the fake's call log so we can assert it wasn't called.
        fake_model.calls = []
        result = await worker.transcribe_synchronously([chunk])
        assert len(result.segments) == 1
        assert result.segments[0].text == "prior segment"
        # Critical: the model was NOT called for the already-transcribed chunk.
        assert fake_model.calls == []
    finally:
        await worker.stop()


async def test_transcribe_synchronously_pauses_normal_loop(
    db, cfg, reporter, monkeypatch
):
    """While ``transcribe_synchronously`` runs the pause flag is True."""

    seen_pause: list[bool] = []

    class _ObservingModel(_FakeModel):
        def transcribe(self, path, **kwargs):
            # Read the pause flag from the closure-captured worker.
            seen_pause.append(worker._pause_normal_loop)
            return super().transcribe(path, **kwargs)

    fake_model = _ObservingModel(segments=[{"start": 0, "end": 1, "text": "x"}])
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        sid, cid = await _make_session_with_chunk(db, channel="mic")
        chunk = await queries.get_chunk_by_id(db, cid)
        assert chunk is not None
        await worker.transcribe_synchronously([chunk])
        # The pause flag was True at the moment the model ran.
        assert seen_pause and seen_pause[0] is True
        # And it's been cleared by the time we return.
        assert worker._pause_normal_loop is False
    finally:
        await worker.stop()


async def test_transcribe_synchronously_serialised_via_lock(
    db, cfg, reporter, monkeypatch
):
    """Two concurrent calls cannot enter the critical section at the same time."""

    in_flight = 0
    max_in_flight = 0

    class _SlowModel(_FakeModel):
        def transcribe(self, path, **kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            # Simulate a small amount of GPU work so concurrency would be
            # observable if the lock were missing.
            import time as _time

            _time.sleep(0.05)
            in_flight -= 1
            return super().transcribe(path, **kwargs)

    fake_model = _SlowModel(segments=[{"start": 0, "end": 1, "text": "x"}])
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        # Two distinct chunks so each call has work to do.
        sid_a, cid_a = await _make_session_with_chunk(
            db, channel="mic", path="/tmp/a.wav", start_ts=1_000.0
        )
        sid_b, cid_b = await _make_session_with_chunk(
            db, channel="mic", path="/tmp/b.wav", start_ts=2_000.0
        )
        chunk_a = await queries.get_chunk_by_id(db, cid_a)
        chunk_b = await queries.get_chunk_by_id(db, cid_b)
        assert chunk_a is not None and chunk_b is not None

        await asyncio.gather(
            worker.transcribe_synchronously([chunk_a]),
            worker.transcribe_synchronously([chunk_b]),
        )
        # If serialisation works, max_in_flight == 1.
        assert max_in_flight == 1
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_stop_is_idempotent(db, cfg, reporter, monkeypatch):
    fake = _FakeWhisperX()
    _install_fake_whisperx(monkeypatch, fake)
    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
    await worker.stop()
    # Second stop is a no-op (and must not raise).
    await worker.stop()
    assert worker._loop_task is None
    assert worker._load_task is None


async def test_start_is_idempotent(db, cfg, reporter, monkeypatch):
    fake = _FakeWhisperX()
    _install_fake_whisperx(monkeypatch, fake)
    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    first_task = worker._loop_task
    # Calling start() again should not spawn a second task.
    await worker.start()
    assert worker._loop_task is first_task
    await worker.stop()


# ---------------------------------------------------------------------------
# Word timestamp normalization
# ---------------------------------------------------------------------------


async def test_word_timestamps_serialised_to_json(
    db, cfg, reporter, monkeypatch
):
    fake_model = _FakeModel(
        segments=[
            {
                "start": 0.0,
                "end": 1.0,
                "text": "hi",
                "words": [
                    {"word": "hi", "start": 0.0, "end": 1.0, "probability": 0.9}
                ],
            }
        ]
    )
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        sid, cid = await _make_session_with_chunk(db, channel="mic")
        chunk = await queries.get_chunk_by_id(db, cid)
        assert chunk is not None
        await worker._process_chunk(chunk)
        rows = await queries.get_segments_for_chunk(db, cid)
        assert len(rows) == 1
        assert rows[0].words is not None
        decoded = json.loads(rows[0].words)
        assert decoded[0]["word"] == "hi"
        # Timestamps should be shifted into epoch (chunk.start_ts == 1_000.0).
        assert decoded[0]["start"] == 1_000.0
        assert decoded[0]["end"] == 1_001.0
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# Empty / silent chunk handling in synchronous path
# ---------------------------------------------------------------------------


async def test_sync_silent_chunk_marks_transcribed_no_segments(
    db, cfg, reporter, monkeypatch
):
    """A chunk with no recognised speech should still be marked transcribed.

    Without this the periodic worker would loop forever picking it up.
    """
    fake_model = _FakeModel(segments=[])  # empty segment list
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        sid, cid = await _make_session_with_chunk(db, channel="mic")
        chunk = await queries.get_chunk_by_id(db, cid)
        assert chunk is not None
        result = await worker.transcribe_synchronously([chunk])
        assert result.segments == []
        marked = await queries.get_chunk_by_id(db, cid)
        assert marked is not None
        assert marked.status == "transcribed"
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# Word-level alignment (Phase 3 follow-up)
# ---------------------------------------------------------------------------


async def test_alignment_replaces_segments_when_align_model_loaded(
    db, cfg, reporter, monkeypatch
):
    """When the alignment model loads, post-transcribe align replaces segments.

    WhisperX 3.x splits per-word timestamps into a separate ``align()``
    step. The worker loads an alignment model after the main one and,
    when ``cfg.transcription.word_timestamps`` is True, runs align after
    each transcribe call. The aligned result's ``segments`` (with their
    new ``words`` arrays) should overwrite the originals.
    """
    fake_model = _FakeModel(
        segments=[
            {"start": 0.0, "end": 1.0, "text": "hi", "words": None}
        ]
    )
    fake = _FakeWhisperX(model=fake_model)

    align_calls: list[dict] = []

    def _fake_load_align(language_code, device, *args, **kwargs):
        return ("ALIGN_MODEL", {"language": language_code, "device": device})

    def _fake_align(transcript, model, metadata, audio, device, **kwargs):
        align_calls.append(
            {
                "model": model,
                "metadata": metadata,
                "audio": audio,
                "device": device,
            }
        )
        # Return aligned segments with word stamps populated.
        return {
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "hi",
                    "words": [
                        {"word": "hi", "start": 0.05, "end": 0.95, "score": 0.9}
                    ],
                }
            ],
            "word_segments": [],
        }

    fake.load_align_model = _fake_load_align
    fake.align = _fake_align
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        # Wait for the align model to load too — it loads after _model_ready.
        await _wait_for(lambda: worker._align_model is not None, timeout=2.0)

        sid, cid = await _make_session_with_chunk(db, channel="mic")
        chunk = await queries.get_chunk_by_id(db, cid)
        assert chunk is not None
        await worker._process_chunk(chunk)

        rows = await queries.get_segments_for_chunk(db, cid)
        assert len(rows) == 1
        assert rows[0].words is not None
        decoded = json.loads(rows[0].words)
        assert decoded[0]["word"] == "hi"
        # Confirm align was actually called (rather than the test passing
        # because the un-aligned segments happened to have words).
        assert len(align_calls) == 1
        assert align_calls[0]["model"] == "ALIGN_MODEL"
        assert align_calls[0]["device"] == "cpu"
    finally:
        await worker.stop()


async def test_align_failure_falls_back_to_unaligned_segments(
    db, cfg, reporter, monkeypatch
):
    """If ``whisperx.align`` raises, transcription returns unaligned segments.

    Best-effort policy: a broken alignment model must never break the
    transcription pipeline. Segments without ``words`` still flow.
    """
    fake_model = _FakeModel(
        segments=[
            {"start": 0.0, "end": 1.0, "text": "hello", "words": None}
        ]
    )
    fake = _FakeWhisperX(model=fake_model)

    fake.load_align_model = lambda *args, **kwargs: ("ALIGN_MODEL", {})

    def _broken_align(*args, **kwargs):
        raise RuntimeError("align blew up")

    fake.align = _broken_align
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        await _wait_for(lambda: worker._align_model is not None, timeout=2.0)

        sid, cid = await _make_session_with_chunk(db, channel="mic")
        chunk = await queries.get_chunk_by_id(db, cid)
        assert chunk is not None
        await worker._process_chunk(chunk)

        rows = await queries.get_segments_for_chunk(db, cid)
        assert len(rows) == 1
        # Segment persisted with no word-level data — alignment failed.
        assert rows[0].text == "hello"
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# WAV pre-decoding (no-ffmpeg path)
# ---------------------------------------------------------------------------


def test_load_wav_mono_16k_round_trip(tmp_path):
    """``_load_wav_mono_16k`` decodes int16 PCM into float32 in [-1, 1]."""
    wav_path = tmp_path / "tone.wav"
    sr = 16_000
    samples = np.array(
        [0, 16384, -16384, 32767, -32768, 0], dtype=np.int16
    )
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())

    audio = _load_wav_mono_16k(str(wav_path))

    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert audio.shape == (len(samples),)
    expected = (samples.astype(np.float32) / 32768.0)
    np.testing.assert_allclose(audio, expected, rtol=0, atol=0)


def test_load_wav_mono_16k_rejects_wrong_format(tmp_path):
    """Stereo / wrong-rate / wrong-width WAVs raise rather than silently miss."""
    wav_path = tmp_path / "stereo.wav"
    samples = np.zeros(160, dtype=np.int16)
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(2)  # stereo — not what whisper expects
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(samples.tobytes())

    with pytest.raises(ValueError, match="expected 16-bit mono"):
        _load_wav_mono_16k(str(wav_path))


async def test_transcribe_chunk_passes_ndarray_not_path(
    db, cfg, reporter, monkeypatch
):
    """The model receives a numpy array — never a path string.

    Regression guard: if a future refactor re-introduces path-based
    transcription, WhisperX will subprocess-exec ``ffmpeg`` inside the
    bundled daemon, which has no ffmpeg on PATH. Every voice note then
    fails with ``[Errno 2] No such file or directory: 'ffmpeg'``.
    """
    fake_model = _FakeModel(
        segments=[{"start": 0.0, "end": 0.1, "text": "ok"}]
    )
    fake = _FakeWhisperX(model=fake_model)
    _install_fake_whisperx(monkeypatch, fake)

    worker = _make_worker(db=db, cfg=cfg, reporter=reporter)
    await worker.start()
    try:
        await asyncio.wait_for(worker._model_ready.wait(), timeout=2.0)
        sid, cid = await _make_session_with_chunk(db, channel="mic")
        chunk = await queries.get_chunk_by_id(db, cid)
        assert chunk is not None
        await worker._process_chunk(chunk)

        assert len(fake_model.calls) == 1
        audio_arg, _kwargs = fake_model.calls[0]
        assert isinstance(audio_arg, np.ndarray), (
            f"model received {type(audio_arg).__name__} instead of ndarray"
        )
        assert audio_arg.dtype == np.float32
        assert audio_arg.ndim == 1
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# Slow / real-WhisperX integration test (gated)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_real_whisperx_handles_silence(tmp_path):
    """Run an actual WhisperX model on a 5s sine wave; should not crash.

    Skipped unless transcription extras are installed and ``-m slow`` is
    selected. We don't assert on the produced text — silence may transcribe
    as nothing or as garbage; we just want to confirm the wiring works.
    """
    pytest.importorskip("whisperx")
    import wave

    import numpy as np
    import whisperx  # type: ignore[import-not-found]

    wav_path = tmp_path / "sine.wav"
    sr = 16_000
    n_samples = sr * 5
    t = np.arange(n_samples) / sr
    samples = (np.sin(2 * np.pi * 440 * t) * 0.1 * 32767).astype(np.int16)
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())

    model = whisperx.load_model(
        "tiny.en", device="cpu", compute_type="int8", language="en"
    )
    result = model.transcribe(
        str(wav_path), language="en", vad_filter=True, word_timestamps=False
    )
    # We don't care about content, just that the call returns a usable dict.
    assert isinstance(result, dict)
    assert "segments" in result
