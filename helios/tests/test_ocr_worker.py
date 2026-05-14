"""Tests for :class:`helios.workers.ocr.OcrWorker` — Track 5A.

Strategy:

* Replace the Vision-framework backend by injecting an ``ocr_callable``
  at construction time (the real path is exercised by ``-m slow`` only,
  and the worker stores raw OCR text, so testing here would mean
  shipping screenshot fixtures we don't need).
* The video source is a small in-memory async generator wrapper so we
  control frame timing deterministically.
* Database fixtures from :mod:`tests.conftest` give us a real schema —
  the worker writes ``ocr_frames`` rows we read back to assert.
* Frontmost bundle id is injected as a callable returning a canned value.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import AsyncIterator

import pytest
import pytest_asyncio

from helios.clock import VirtualClock
from helios.components import ComponentStatusReporter
from helios.config import HeliosConfig
from helios.db import queries
from helios.db.migrations import run_migrations
from helios.sources.interface import VideoFrame
from helios.state import DaemonStateMachine
from helios.workers.ocr import (
    OcrResult,
    OcrWorker,
    compute_phash,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _OcrCall:
    jpeg: bytes
    result: OcrResult | None


class _FakeOcr:
    """Records calls and returns canned results in order.

    Construction takes an iterable of ``OcrResult | None`` values; each
    call consumes one in FIFO order. When exhausted, returns ``None``
    (mirrors a low-confidence Vision response).
    """

    def __init__(self, results: list[OcrResult | None] | None = None) -> None:
        self._results = list(results or [])
        self.calls: list[bytes] = []

    async def __call__(self, jpeg: bytes) -> OcrResult | None:
        self.calls.append(jpeg)
        if not self._results:
            return None
        return self._results.pop(0)


class _FakeVideoSource:
    """Plays back a fixed list of ``VideoFrame`` through ``video_frames()``.

    The iterator yields all frames then waits forever — the worker's
    ``stop()`` cancels its loop task, which unwinds the ``async for``
    naturally. This matches the production
    :class:`helios.sources.real.SCKSystemAudioSource` shape: a sentinel
    on stop. We don't need the sentinel here because tests cancel
    explicitly.
    """

    def __init__(self, frames: list[VideoFrame]) -> None:
        self._frames = list(frames)

    async def video_frames(self) -> AsyncIterator[VideoFrame]:
        for frame in self._frames:
            yield frame
        # Sleep forever — emulates an idle producer post-burst.
        await asyncio.sleep(3600)


def _make_session_provider(
    session_id: int | None,
    override_until: float | None = None,
):
    """Return an async callable matching ``ActiveSessionProvider``."""

    async def provider():
        if session_id is None:
            return None
        return (session_id, override_until)

    return provider


def _make_worker(
    *,
    db,
    cfg,
    reporter,
    video_source,
    session_provider,
    frontmost_bundle: str | None = "com.microsoft.teams2",
    ocr_callable=None,
    clock=None,
) -> OcrWorker:
    if clock is None:
        clock = VirtualClock(initial_ts=1_000.0)
    return OcrWorker(
        db=db,
        config=cfg,
        clock=clock,
        reporter=reporter,
        video_source=video_source,
        active_session_provider=session_provider,
        frontmost_provider=lambda: frontmost_bundle,
        ocr_callable=ocr_callable,
    )


async def _wait_for(predicate, *, timeout: float = 1.0, step: float = 0.01) -> None:
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


# ---------------------------------------------------------------------------
# Lifecycle + Vision availability
# ---------------------------------------------------------------------------


async def test_start_with_injected_callable_reports_ok(
    db, cfg, reporter, monkeypatch
):
    """When ``ocr_callable`` is provided, ``start`` skips the Vision probe."""
    ocr = _FakeOcr()
    video = _FakeVideoSource([])
    session = _make_session_provider(session_id=1)
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=video, session_provider=_make_session_provider(sid),
        ocr_callable=ocr,
    )
    await worker.start()
    try:
        rec = reporter.get("ocr")
        assert rec is not None
        assert rec.status == "ok"
    finally:
        await worker.stop()


async def test_start_unavailable_when_vision_missing(
    db, cfg, reporter, monkeypatch
):
    """Lazy ``import Vision`` raising ⇒ unavailable, not error."""
    # ``None`` in sys.modules sentinels ImportError on the lazy import.
    monkeypatch.setitem(sys.modules, "Vision", None)

    video = _FakeVideoSource([])
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=video,
        session_provider=_make_session_provider(None),
        ocr_callable=None,  # force the Vision probe
    )
    await worker.start()
    try:
        rec = reporter.get("ocr")
        assert rec is not None
        assert rec.status == "unavailable"
        assert rec.reason == "vision_not_available"
        # Worker did not spawn a loop.
        assert worker._loop_task is None
    finally:
        await worker.stop()


async def test_start_unavailable_when_config_disabled(
    db, cfg, reporter
):
    cfg.ocr.enabled = False
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=_FakeVideoSource([]),
        session_provider=_make_session_provider(None),
        ocr_callable=_FakeOcr(),
    )
    await worker.start()
    try:
        rec = reporter.get("ocr")
        assert rec is not None
        assert rec.status == "unavailable"
        assert rec.reason == "disabled"
        assert worker._loop_task is None
    finally:
        await worker.stop()


async def test_start_and_stop_are_idempotent(db, cfg, reporter):
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=_FakeVideoSource([]),
        session_provider=_make_session_provider(None),
        ocr_callable=_FakeOcr(),
    )
    await worker.start()
    first = worker._loop_task
    await worker.start()
    assert worker._loop_task is first
    await worker.stop()
    # Second stop must not raise.
    await worker.stop()
    assert worker._loop_task is None


# ---------------------------------------------------------------------------
# Gating semantics — observed end-to-end via DB writes
# ---------------------------------------------------------------------------


async def test_frame_persisted_when_frontmost_is_allowed(db, cfg, reporter):
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    text = "x" * cfg.ocr.min_text_chars
    ocr = _FakeOcr([OcrResult(text=text, avg_confidence=0.9)])
    video = _FakeVideoSource([VideoFrame(ts=1_001.0, jpeg=b"jpeg-a")])
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=video,
        session_provider=_make_session_provider(sid),
        frontmost_bundle="com.microsoft.teams2",
        ocr_callable=ocr,
    )
    await worker.start()
    try:
        await _wait_for(lambda: len(ocr.calls) >= 1, timeout=1.0)
        rows = await queries.get_ocr_frames_in_range(
            db, start_ts=0.0, end_ts=2_000.0
        )
        assert len(rows) == 1
        assert rows[0].app_bundle == "com.microsoft.teams2"
        assert rows[0].text == text
        assert rows[0].avg_confidence == pytest.approx(0.9)
    finally:
        await worker.stop()


async def test_frame_dropped_when_frontmost_not_in_allowlist(db, cfg, reporter):
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    ocr = _FakeOcr([OcrResult(text="x" * 50, avg_confidence=0.9)])
    video = _FakeVideoSource([VideoFrame(ts=1_001.0, jpeg=b"jpeg-a")])
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=video,
        session_provider=_make_session_provider(sid),
        frontmost_bundle="com.apple.mail",
        ocr_callable=ocr,
    )
    await worker.start()
    try:
        # Give the loop time to consume the frame.
        await asyncio.sleep(0.1)
        # Vision should never have been invoked because the gate dropped
        # the frame upstream.
        assert ocr.calls == []
        rows = await queries.get_ocr_frames_in_range(
            db, start_ts=0.0, end_ts=2_000.0
        )
        assert rows == []
    finally:
        await worker.stop()


async def test_override_opens_gate_for_disallowed_app(db, cfg, reporter):
    """An active ``screen_capture_override_until`` beats the allowlist."""
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    text = "y" * cfg.ocr.min_text_chars
    ocr = _FakeOcr([OcrResult(text=text, avg_confidence=0.8)])
    video = _FakeVideoSource([VideoFrame(ts=1_001.0, jpeg=b"jpeg-a")])
    # Clock at 1_000.0, override expires at 1_500.0 ⇒ gate is open.
    clock = VirtualClock(initial_ts=1_000.0)
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=video,
        session_provider=_make_session_provider(sid, override_until=1_500.0),
        frontmost_bundle="com.apple.mail",  # NOT in allowlist
        ocr_callable=ocr,
        clock=clock,
    )
    await worker.start()
    try:
        await _wait_for(lambda: len(ocr.calls) >= 1, timeout=1.0)
        rows = await queries.get_ocr_frames_in_range(
            db, start_ts=0.0, end_ts=2_000.0
        )
        assert len(rows) == 1
        assert rows[0].app_bundle == "com.apple.mail"
    finally:
        await worker.stop()


async def test_no_active_session_drops_frame(db, cfg, reporter):
    """Without a session, the frame can't be attributed — drop it."""
    ocr = _FakeOcr([OcrResult(text="x" * 50, avg_confidence=0.9)])
    video = _FakeVideoSource([VideoFrame(ts=1_001.0, jpeg=b"jpeg-a")])
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=video,
        session_provider=_make_session_provider(None),
        ocr_callable=ocr,
    )
    await worker.start()
    try:
        await asyncio.sleep(0.1)
        assert ocr.calls == []
        rows = await queries.get_ocr_frames_in_range(
            db, start_ts=0.0, end_ts=2_000.0
        )
        assert rows == []
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# Throttling + dedup
# ---------------------------------------------------------------------------


async def test_fps_throttle_drops_frames_within_interval(db, cfg, reporter):
    """At fps=1 we expect at most one frame per second of capture time."""
    cfg.ocr.fps = 1
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    text = "z" * cfg.ocr.min_text_chars
    # Send three frames whose timestamps fall within the same 1s window.
    frames = [
        VideoFrame(ts=1_001.0, jpeg=b"jpeg-a"),
        VideoFrame(ts=1_001.5, jpeg=b"jpeg-b"),
        VideoFrame(ts=1_001.8, jpeg=b"jpeg-c"),
    ]
    ocr = _FakeOcr([
        OcrResult(text=text + "1", avg_confidence=0.9),
        OcrResult(text=text + "2", avg_confidence=0.9),
        OcrResult(text=text + "3", avg_confidence=0.9),
    ])
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=_FakeVideoSource(frames),
        session_provider=_make_session_provider(sid),
        ocr_callable=ocr,
    )
    await worker.start()
    try:
        # First frame should be processed; subsequent two skipped at
        # the throttle stage before reaching Vision.
        await _wait_for(lambda: len(ocr.calls) >= 1, timeout=1.0)
        await asyncio.sleep(0.1)
        assert len(ocr.calls) == 1
        rows = await queries.get_ocr_frames_in_range(
            db, start_ts=0.0, end_ts=2_000.0
        )
        assert len(rows) == 1
    finally:
        await worker.stop()


async def test_fps_throttle_passes_frames_outside_interval(db, cfg, reporter):
    cfg.ocr.fps = 1
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    text = "z" * cfg.ocr.min_text_chars
    # Three frames spaced > 1s apart ⇒ all should pass the throttle.
    frames = [
        VideoFrame(ts=1_001.0, jpeg=b"jpeg-a"),
        VideoFrame(ts=1_003.0, jpeg=b"jpeg-b"),
        VideoFrame(ts=1_005.0, jpeg=b"jpeg-c"),
    ]
    ocr = _FakeOcr([
        OcrResult(text=text + "1", avg_confidence=0.9),
        OcrResult(text=text + "2", avg_confidence=0.9),
        OcrResult(text=text + "3", avg_confidence=0.9),
    ])
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=_FakeVideoSource(frames),
        session_provider=_make_session_provider(sid),
        ocr_callable=ocr,
    )
    await worker.start()
    try:
        await _wait_for(lambda: len(ocr.calls) >= 3, timeout=1.0)
        rows = await queries.get_ocr_frames_in_range(
            db, start_ts=0.0, end_ts=2_000.0
        )
        assert len(rows) == 3
    finally:
        await worker.stop()


async def test_near_duplicate_frames_deduped(db, cfg, reporter):
    """Identical JPEG bytes ⇒ Hamming distance 0 ⇒ drop the duplicate."""
    cfg.ocr.fps = 1
    cfg.ocr.dedup_hamming_threshold = 4
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    text = "q" * cfg.ocr.min_text_chars
    frames = [
        VideoFrame(ts=1_001.0, jpeg=b"identical-jpeg"),
        VideoFrame(ts=1_003.0, jpeg=b"identical-jpeg"),  # duplicate
        VideoFrame(ts=1_005.0, jpeg=b"different-jpeg"),  # passes
    ]
    ocr = _FakeOcr([
        OcrResult(text=text + "1", avg_confidence=0.9),
        OcrResult(text=text + "2", avg_confidence=0.9),
        OcrResult(text=text + "3", avg_confidence=0.9),
    ])
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=_FakeVideoSource(frames),
        session_provider=_make_session_provider(sid),
        ocr_callable=ocr,
    )
    await worker.start()
    try:
        await _wait_for(
            lambda: len(ocr.calls) >= 3, timeout=1.0
        )
        rows = await queries.get_ocr_frames_in_range(
            db, start_ts=0.0, end_ts=2_000.0
        )
        # 1st + 3rd persist; 2nd dropped as near-duplicate.
        assert len(rows) == 2
        assert rows[0].text.endswith("1")
        assert rows[1].text.endswith("3")
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# OCR result filters
# ---------------------------------------------------------------------------


async def test_no_ocr_result_drops_frame(db, cfg, reporter):
    """Vision returning ``None`` (low confidence / no text) ⇒ no row."""
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    ocr = _FakeOcr([None])  # Vision yields nothing useful
    video = _FakeVideoSource([VideoFrame(ts=1_001.0, jpeg=b"jpeg-a")])
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=video,
        session_provider=_make_session_provider(sid),
        ocr_callable=ocr,
    )
    await worker.start()
    try:
        await _wait_for(lambda: len(ocr.calls) >= 1, timeout=1.0)
        rows = await queries.get_ocr_frames_in_range(
            db, start_ts=0.0, end_ts=2_000.0
        )
        assert rows == []
    finally:
        await worker.stop()


async def test_short_text_drops_frame(db, cfg, reporter):
    """Text shorter than ``min_text_chars`` is dropped as noise."""
    cfg.ocr.min_text_chars = 20
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    ocr = _FakeOcr([OcrResult(text="too short", avg_confidence=0.95)])
    video = _FakeVideoSource([VideoFrame(ts=1_001.0, jpeg=b"jpeg-a")])
    worker = _make_worker(
        db=db, cfg=cfg, reporter=reporter,
        video_source=video,
        session_provider=_make_session_provider(sid),
        ocr_callable=ocr,
    )
    await worker.start()
    try:
        await _wait_for(lambda: len(ocr.calls) >= 1, timeout=1.0)
        rows = await queries.get_ocr_frames_in_range(
            db, start_ts=0.0, end_ts=2_000.0
        )
        assert rows == []
    finally:
        await worker.stop()


# ---------------------------------------------------------------------------
# DB layer + endpoint integration
# ---------------------------------------------------------------------------


async def test_insert_and_fetch_ocr_frames(db):
    """End-to-end DB round-trip for the query helpers."""
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    await queries.insert_ocr_frame(
        db,
        session_id=sid,
        ts=1_001.0,
        app_bundle="com.microsoft.teams2",
        display_id=1,
        phash=compute_phash(b"a"),
        text="hello world this is OCR",
        avg_confidence=0.8,
    )
    await queries.insert_ocr_frame(
        db,
        session_id=sid,
        ts=1_002.0,
        app_bundle="us.zoom.xos",
        display_id=None,
        phash=compute_phash(b"b"),
        text="second frame text payload",
        avg_confidence=0.4,  # below default threshold
    )
    # Default min_confidence = None ⇒ both rows returned.
    rows = await queries.get_ocr_frames_in_range(
        db, start_ts=0.0, end_ts=2_000.0
    )
    assert len(rows) == 2
    # Filter on min_confidence.
    high = await queries.get_ocr_frames_in_range(
        db, start_ts=0.0, end_ts=2_000.0, min_confidence=0.5
    )
    assert len(high) == 1
    assert high[0].app_bundle == "com.microsoft.teams2"
    # Filter on app_bundle.
    zoom = await queries.get_ocr_frames_in_range(
        db, start_ts=0.0, end_ts=2_000.0, app_bundle="us.zoom.xos"
    )
    assert len(zoom) == 1
    # Range narrowing.
    narrow = await queries.get_ocr_frames_in_range(
        db, start_ts=1_001.5, end_ts=2_000.0
    )
    assert len(narrow) == 1
    assert narrow[0].ts == pytest.approx(1_002.0)


async def test_recent_phashes_helper_returns_newest_first(db):
    sid = await queries.create_session(db, "manual_screen", started_at=1_000.0)
    hashes = [compute_phash(p) for p in (b"a", b"b", b"c")]
    for ts, ph in zip([1_001.0, 1_002.0, 1_003.0], hashes):
        await queries.insert_ocr_frame(
            db,
            session_id=sid,
            ts=ts,
            app_bundle="com.x",
            display_id=None,
            phash=ph,
            text="some long ocr text here",
            avg_confidence=0.9,
        )
    recent = await queries.get_recent_ocr_frame_phashes(
        db, session_id=sid, limit=2
    )
    # Most recent first ⇒ matches the c, b order.
    assert recent[0] == hashes[2]
    assert recent[1] == hashes[1]
    assert len(recent) == 2
