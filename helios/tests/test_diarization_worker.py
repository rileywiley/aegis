"""Tests for helios.workers.diarization (Wave 2F).

Coverage:

* ``start()`` reports the correct ``unavailable`` reason for each
  configuration / environment failure mode (disabled, missing token,
  missing pyannote, model load exception).
* The full happy path: a fake pyannote pipeline yields several turns
  with two unique speakers; the worker writes one ``diarization_turns``
  row per turn with timestamps offset by the session's first chunk
  ``start_ts``.
* Speaker embeddings are stored as float32 BLOBs when configured AND
  the pipeline yields embeddings; otherwise NULL.
* Sessions with no system-channel chunks are marked
  ``not_applicable``.
* Pipeline-side exceptions flip ``diarization_status='failed'``.
* The WAV concatenation helper produces a single, well-formed WAV.
* ``stop()`` is idempotent and cancels both background tasks.

All slow paths use mocks; a single ``@pytest.mark.slow`` test exercises
the real pyannote pipeline against a multi-speaker fixture and is
``SKIP``ed when no HF token is configured.
"""

from __future__ import annotations

import asyncio
import sys
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import pytest_asyncio

from helios.components import ComponentStatusReporter
from helios.config import DiarizationConfig, HeliosConfig
from helios.db import queries
from helios.db.migrations import run_migrations
from helios.state import DaemonStateMachine
from helios.workers.diarization import (
    DiarizationWorker,
    _concat_wavs_blocking,
    _extract_embedding_blob,
)

from .conftest import synth_wav

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
def enabled_config() -> HeliosConfig:
    """Config with diarization.enabled=True (default config disables it)."""
    cfg = HeliosConfig()
    cfg.diarization = DiarizationConfig(
        enabled=True,
        min_speakers=1,
        max_speakers=4,
        store_embeddings=True,
    )
    return cfg


@pytest.fixture
def disabled_config() -> HeliosConfig:
    cfg = HeliosConfig()
    cfg.diarization = DiarizationConfig(enabled=False)
    return cfg


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTurn:
    """Stand-in for pyannote's ``Segment`` (it has ``.start`` and ``.end``)."""

    __slots__ = ("start", "end")

    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end


class _FakeAnnotation:
    """Stand-in for pyannote's ``Annotation``.

    Implements ``itertracks(yield_label=True)`` which yields
    ``(turn, track_id, speaker)`` tuples, plus an optional
    ``speaker_embeddings`` mapping.
    """

    def __init__(
        self,
        turns: list[tuple[float, float, str]],
        embeddings: dict[str, list[float]] | None = None,
    ) -> None:
        self._turns = turns
        self.speaker_embeddings = embeddings if embeddings is not None else None

    def itertracks(self, yield_label: bool = False):
        if not yield_label:
            for i, (s, e, _) in enumerate(self._turns):
                yield _FakeTurn(s, e), str(i)
            return
        for i, (s, e, spk) in enumerate(self._turns):
            yield _FakeTurn(s, e), str(i), spk


class _FakePipeline:
    """Callable returning a configured _FakeAnnotation when invoked."""

    def __init__(self, annotation: _FakeAnnotation) -> None:
        self._annotation = annotation
        self.calls: list[tuple] = []

    def __call__(self, wav_path: str, *, min_speakers: int, max_speakers: int):
        self.calls.append((wav_path, min_speakers, max_speakers))
        return self._annotation


class _ExplodingPipeline:
    """Pipeline that always raises — exercises the failure path."""

    def __call__(self, *args, **kwargs):  # noqa: D401, ANN002, ANN003
        raise RuntimeError("pyannote inference exploded")


# ---------------------------------------------------------------------------
# start() — config / env failure modes
# ---------------------------------------------------------------------------


async def test_start_disabled_reports_unavailable(
    db, reporter, virtual_clock, disabled_config
):
    worker = DiarizationWorker(db, disabled_config, virtual_clock, reporter)
    await worker.start()

    rec = reporter.get("diarization")
    assert rec is not None
    assert rec.status == "unavailable"
    assert rec.reason == "disabled"
    # No tasks should have been spawned.
    assert worker._loop_task is None
    assert worker._load_task is None


async def test_start_missing_token_reports_unavailable(
    db, reporter, virtual_clock, enabled_config
):
    with patch("helios.workers.diarization.get_hf_token", return_value=None):
        worker = DiarizationWorker(db, enabled_config, virtual_clock, reporter)
        await worker.start()

    rec = reporter.get("diarization")
    assert rec is not None
    assert rec.status == "unavailable"
    assert rec.reason == "token_missing"
    assert rec.action and "set_hf_token" in rec.action
    assert worker._loop_task is None


async def test_start_pyannote_not_installed_reports_unavailable(
    db, reporter, virtual_clock, enabled_config
):
    """Simulate an ImportError raised inside the lazy pyannote import."""
    with patch("helios.workers.diarization.get_hf_token", return_value="hf_xxx"):
        # Block the pyannote.audio import by removing it from sys.modules
        # and inserting a meta path finder that raises.
        original = sys.modules.pop("pyannote.audio", None)
        original_pyannote = sys.modules.pop("pyannote", None)

        class _BlockingFinder:
            def find_spec(self, fullname, path=None, target=None):
                if fullname.startswith("pyannote"):
                    raise ImportError("blocked for test")
                return None

        finder = _BlockingFinder()
        sys.meta_path.insert(0, finder)
        try:
            worker = DiarizationWorker(db, enabled_config, virtual_clock, reporter)
            await worker.start()

            # Wait for the background load task to surface the import error.
            for _ in range(50):
                rec = reporter.get("diarization")
                if rec is not None and rec.reason == "pyannote_not_installed":
                    break
                await asyncio.sleep(0.01)

            rec = reporter.get("diarization")
            assert rec is not None
            assert rec.status == "unavailable"
            assert rec.reason == "pyannote_not_installed"
            assert rec.action and "transcription" in rec.action
        finally:
            sys.meta_path.remove(finder)
            if original is not None:
                sys.modules["pyannote.audio"] = original
            if original_pyannote is not None:
                sys.modules["pyannote"] = original_pyannote
            await worker.stop()


async def test_start_model_load_exception_reports_error(
    db, reporter, state_machine, virtual_clock, enabled_config
):
    """Pipeline.from_pretrained raises → reporter records error/model_load_failed."""
    fake_module = type(sys)("pyannote.audio")
    fake_pyannote = type(sys)("pyannote")

    class _BoomPipeline:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("model files are missing")

    fake_module.Pipeline = _BoomPipeline  # type: ignore[attr-defined]

    sys.modules["pyannote"] = fake_pyannote
    sys.modules["pyannote.audio"] = fake_module
    try:
        with patch(
            "helios.workers.diarization.get_hf_token", return_value="hf_xxx"
        ):
            worker = DiarizationWorker(
                db, enabled_config, virtual_clock, reporter
            )
            await worker.start()

            for _ in range(50):
                rec = reporter.get("diarization")
                if rec is not None and rec.reason == "model_load_failed":
                    break
                await asyncio.sleep(0.01)

            rec = reporter.get("diarization")
            assert rec is not None
            assert rec.status == "error"
            assert rec.reason == "model_load_failed"
            # State machine must reflect the error.
            assert "diarization" in state_machine.current().component_errors
            await worker.stop()
    finally:
        sys.modules.pop("pyannote.audio", None)
        sys.modules.pop("pyannote", None)


# ---------------------------------------------------------------------------
# Happy path — full diarization flow
# ---------------------------------------------------------------------------


async def _seed_session_with_chunks(
    db, tmp_path: Path, n_chunks: int = 2
) -> tuple[int, list[Path]]:
    """Create a session + system-channel WAVs + chunk rows. Returns (id, paths)."""
    session_id = await queries.create_session(
        db, "calendar", started_at=10_000.0
    )
    paths: list[Path] = []
    for i in range(n_chunks):
        wav = tmp_path / f"chunk_{i}.wav"
        synth_wav(wav, duration_s=0.5, freq_hz=440 + 100 * i)
        paths.append(wav)
        await queries.insert_audio_chunk(
            db,
            session_id=session_id,
            channel="system",
            start_ts=10_000.0 + i * 30.0,
            end_ts=10_000.0 + (i + 1) * 30.0,
            path=str(wav),
            samples=8000,
        )
    return session_id, paths


async def _wait_for_diarization_status(
    db, session_id: int, expected: str, timeout: float = 3.0
) -> str:
    """Poll until the session's diarization_status reaches ``expected``."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        sess = await queries.get_session_by_id(db, session_id)
        if sess and sess.diarization_status == expected:
            return sess.diarization_status
        await asyncio.sleep(0.02)
    sess = await queries.get_session_by_id(db, session_id)
    return sess.diarization_status if sess else "MISSING"


async def test_happy_path_writes_turns_with_offset_timestamps(
    db, reporter, virtual_clock, enabled_config, tmp_path
):
    """Three turns, two unique speakers → three diarization_turns rows."""
    session_id, _paths = await _seed_session_with_chunks(db, tmp_path)

    fake_pipeline = _FakePipeline(
        _FakeAnnotation(
            turns=[
                (0.0, 5.0, "SPEAKER_00"),
                (5.0, 12.0, "SPEAKER_01"),
                (12.0, 20.0, "SPEAKER_00"),
            ]
        )
    )

    with patch(
        "helios.workers.diarization.get_hf_token", return_value="hf_xxx"
    ):
        worker = DiarizationWorker(
            db, enabled_config, virtual_clock, reporter
        )
        # Skip the heavy pyannote import + load; inject the fake pipeline
        # directly and signal readiness.
        worker._pipeline = fake_pipeline
        worker._pipeline_ready.set()
        worker._loop_task = asyncio.create_task(worker._run_loop())

        await worker.enqueue_session(session_id)
        status = await _wait_for_diarization_status(db, session_id, "complete")
        assert status == "complete"

        await worker.stop()

    turns = await queries.get_diarization_turns_for_session(db, session_id)
    assert len(turns) == 3
    base = 10_000.0
    # Speaker labels round-trip.
    assert [t.speaker_label for t in turns] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_00",
    ]
    # Timestamps are offset by the first chunk's start_ts.
    assert turns[0].start_ts == base + 0.0
    assert turns[0].end_ts == base + 5.0
    assert turns[1].start_ts == base + 5.0
    assert turns[1].end_ts == base + 12.0
    assert turns[2].end_ts == base + 20.0

    # Pyannote was called with the configured min/max speakers.
    assert fake_pipeline.calls
    _wav, min_s, max_s = fake_pipeline.calls[0]
    assert min_s == 1
    assert max_s == 4


async def test_embeddings_stored_when_pipeline_provides_them(
    db, reporter, virtual_clock, enabled_config, tmp_path
):
    session_id, _ = await _seed_session_with_chunks(db, tmp_path)
    embeddings = {
        "SPEAKER_00": [0.1, 0.2, 0.3, 0.4],
        "SPEAKER_01": [0.5, 0.6, 0.7, 0.8],
    }
    fake_pipeline = _FakePipeline(
        _FakeAnnotation(
            turns=[
                (0.0, 4.0, "SPEAKER_00"),
                (4.0, 8.0, "SPEAKER_01"),
            ],
            embeddings=embeddings,
        )
    )

    with patch(
        "helios.workers.diarization.get_hf_token", return_value="hf_xxx"
    ):
        worker = DiarizationWorker(
            db, enabled_config, virtual_clock, reporter
        )
        worker._pipeline = fake_pipeline
        worker._pipeline_ready.set()
        worker._loop_task = asyncio.create_task(worker._run_loop())

        await worker.enqueue_session(session_id)
        await _wait_for_diarization_status(db, session_id, "complete")
        await worker.stop()

    turns = await queries.get_diarization_turns_for_session(db, session_id)
    assert len(turns) == 2
    # Both turns have embeddings stored as float32 byte strings.
    for t, expected_label in zip(turns, ["SPEAKER_00", "SPEAKER_01"]):
        assert t.embedding is not None
        decoded = np.frombuffer(t.embedding, dtype=np.float32)
        np.testing.assert_allclose(
            decoded, embeddings[expected_label], rtol=1e-6
        )


async def test_embeddings_null_when_pipeline_lacks_them(
    db, reporter, virtual_clock, enabled_config, tmp_path
):
    """``store_embeddings=True`` but pipeline yields no embeddings → NULL column."""
    session_id, _ = await _seed_session_with_chunks(db, tmp_path)
    fake_pipeline = _FakePipeline(
        _FakeAnnotation(
            turns=[(0.0, 3.0, "SPEAKER_00")],
            embeddings=None,  # no speaker_embeddings attached
        )
    )

    with patch(
        "helios.workers.diarization.get_hf_token", return_value="hf_xxx"
    ):
        worker = DiarizationWorker(
            db, enabled_config, virtual_clock, reporter
        )
        worker._pipeline = fake_pipeline
        worker._pipeline_ready.set()
        worker._loop_task = asyncio.create_task(worker._run_loop())

        await worker.enqueue_session(session_id)
        await _wait_for_diarization_status(db, session_id, "complete")
        await worker.stop()

    turns = await queries.get_diarization_turns_for_session(db, session_id)
    assert len(turns) == 1
    assert turns[0].embedding is None


async def test_embeddings_null_when_disabled_in_config(
    db, reporter, virtual_clock, tmp_path
):
    """``store_embeddings=False`` → embedding column always NULL even when available."""
    cfg = HeliosConfig()
    cfg.diarization = DiarizationConfig(
        enabled=True, min_speakers=1, max_speakers=4, store_embeddings=False
    )
    session_id, _ = await _seed_session_with_chunks(db, tmp_path)
    fake_pipeline = _FakePipeline(
        _FakeAnnotation(
            turns=[(0.0, 3.0, "SPEAKER_00")],
            embeddings={"SPEAKER_00": [0.1, 0.2, 0.3]},
        )
    )

    with patch(
        "helios.workers.diarization.get_hf_token", return_value="hf_xxx"
    ):
        worker = DiarizationWorker(db, cfg, virtual_clock, reporter)
        worker._pipeline = fake_pipeline
        worker._pipeline_ready.set()
        worker._loop_task = asyncio.create_task(worker._run_loop())

        await worker.enqueue_session(session_id)
        await _wait_for_diarization_status(db, session_id, "complete")
        await worker.stop()

    turns = await queries.get_diarization_turns_for_session(db, session_id)
    assert len(turns) == 1
    assert turns[0].embedding is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_session_with_no_system_chunks_marked_not_applicable(
    db, reporter, virtual_clock, enabled_config, tmp_path
):
    """A session with only mic chunks (or none) → status='not_applicable'."""
    session_id = await queries.create_session(db, "calendar", started_at=10_000.0)
    # mic-only chunk, no system chunks
    mic_wav = tmp_path / "mic.wav"
    synth_wav(mic_wav, duration_s=0.5)
    await queries.insert_audio_chunk(
        db,
        session_id=session_id,
        channel="mic",
        start_ts=10_000.0,
        end_ts=10_030.0,
        path=str(mic_wav),
        samples=8000,
    )

    fake_pipeline = _FakePipeline(_FakeAnnotation(turns=[]))

    with patch(
        "helios.workers.diarization.get_hf_token", return_value="hf_xxx"
    ):
        worker = DiarizationWorker(
            db, enabled_config, virtual_clock, reporter
        )
        worker._pipeline = fake_pipeline
        worker._pipeline_ready.set()
        worker._loop_task = asyncio.create_task(worker._run_loop())

        await worker.enqueue_session(session_id)
        status = await _wait_for_diarization_status(
            db, session_id, "not_applicable"
        )
        await worker.stop()

    assert status == "not_applicable"
    # No turns inserted, no pipeline call.
    turns = await queries.get_diarization_turns_for_session(db, session_id)
    assert turns == []
    assert fake_pipeline.calls == []


async def test_pipeline_exception_marks_session_failed(
    db, reporter, virtual_clock, enabled_config, tmp_path
):
    session_id, _ = await _seed_session_with_chunks(db, tmp_path)

    with patch(
        "helios.workers.diarization.get_hf_token", return_value="hf_xxx"
    ):
        worker = DiarizationWorker(
            db, enabled_config, virtual_clock, reporter
        )
        worker._pipeline = _ExplodingPipeline()
        worker._pipeline_ready.set()
        worker._loop_task = asyncio.create_task(worker._run_loop())

        await worker.enqueue_session(session_id)
        status = await _wait_for_diarization_status(db, session_id, "failed")
        await worker.stop()

    assert status == "failed"
    turns = await queries.get_diarization_turns_for_session(db, session_id)
    assert turns == []


async def test_start_idempotent(db, reporter, virtual_clock, enabled_config):
    """Calling start() twice while running is a no-op."""
    fake_pipeline = _FakePipeline(_FakeAnnotation(turns=[]))
    with patch(
        "helios.workers.diarization.get_hf_token", return_value="hf_xxx"
    ):
        worker = DiarizationWorker(
            db, enabled_config, virtual_clock, reporter
        )
        worker._pipeline = fake_pipeline
        worker._pipeline_ready.set()
        worker._loop_task = asyncio.create_task(worker._run_loop())

        first_loop = worker._loop_task
        await worker.start()  # should not spawn a second loop
        assert worker._loop_task is first_loop

        await worker.stop()


async def test_stop_idempotent(db, reporter, virtual_clock, enabled_config):
    """Calling stop() twice is safe even when nothing started."""
    worker = DiarizationWorker(db, enabled_config, virtual_clock, reporter)
    await worker.stop()  # never started
    await worker.stop()  # still safe


async def test_stop_cancels_running_load_and_loop(
    db, reporter, virtual_clock, enabled_config
):
    """A worker mid-load + mid-loop tears down promptly on stop()."""

    # Hang the model load forever so stop() must cancel it.
    async def _hung_load():
        await asyncio.Event().wait()

    with patch(
        "helios.workers.diarization.get_hf_token", return_value="hf_xxx"
    ):
        worker = DiarizationWorker(
            db, enabled_config, virtual_clock, reporter
        )
        worker._enabled = True
        worker._load_task = asyncio.create_task(_hung_load())
        worker._loop_task = asyncio.create_task(worker._run_loop())
        await asyncio.sleep(0)  # let tasks start

        await worker.stop()
        assert worker._load_task is None
        assert worker._loop_task is None


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_concat_wavs_blocking_joins_two_files(tmp_path):
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    synth_wav(a, duration_s=0.5, freq_hz=440)
    synth_wav(b, duration_s=0.25, freq_hz=880)

    chunks = [
        # We only need .path to exist; row id fields below are dummies.
        _make_chunk_row(1, 1, "system", 0.0, 0.5, str(a)),
        _make_chunk_row(2, 1, "system", 0.5, 0.75, str(b)),
    ]
    out = tmp_path / "concat.wav"
    _concat_wavs_blocking(chunks, out)
    with wave.open(str(out), "rb") as f:
        n_frames = f.getnframes()
        sr = f.getframerate()
        assert sr == 16000
        # 0.5s + 0.25s of audio at 16kHz = 12000 frames.
        assert n_frames == int(0.75 * sr)


def test_concat_wavs_blocking_raises_when_no_chunks(tmp_path):
    with pytest.raises(ValueError):
        _concat_wavs_blocking([], tmp_path / "x.wav")


def test_concat_wavs_blocking_skips_missing_paths(tmp_path):
    """Chunks pointing to missing files are silently skipped."""
    a = tmp_path / "a.wav"
    synth_wav(a, duration_s=0.4)
    chunks = [
        _make_chunk_row(1, 1, "system", 0.0, 0.4, str(a)),
        _make_chunk_row(2, 1, "system", 0.4, 1.0, str(tmp_path / "nope.wav")),
    ]
    out = tmp_path / "out.wav"
    _concat_wavs_blocking(chunks, out)
    with wave.open(str(out), "rb") as f:
        # Only 0.4s ended up in the concat output.
        assert f.getnframes() == int(0.4 * 16000)


def test_concat_wavs_blocking_raises_when_all_paths_missing(tmp_path):
    chunks = [
        _make_chunk_row(1, 1, "system", 0.0, 0.4, str(tmp_path / "nope.wav")),
    ]
    with pytest.raises(ValueError):
        _concat_wavs_blocking(chunks, tmp_path / "out.wav")


def test_extract_embedding_blob_returns_none_for_missing_speaker():
    ann = _FakeAnnotation(turns=[], embeddings={"SPEAKER_00": [0.1, 0.2]})
    assert _extract_embedding_blob(ann, "SPEAKER_99") is None


def test_extract_embedding_blob_returns_none_when_attr_missing():
    """Annotation without ``speaker_embeddings`` → None."""

    class _Bare:
        pass

    assert _extract_embedding_blob(_Bare(), "SPEAKER_00") is None


def test_extract_embedding_blob_returns_float32_bytes():
    ann = _FakeAnnotation(turns=[], embeddings={"S": [1.0, 2.0, 3.0]})
    blob = _extract_embedding_blob(ann, "S")
    assert blob is not None
    assert len(blob) == 3 * 4  # 3 float32s
    np.testing.assert_allclose(
        np.frombuffer(blob, dtype=np.float32), [1.0, 2.0, 3.0]
    )


# ---------------------------------------------------------------------------
# Slow / integration test (skipped without HF token)
# ---------------------------------------------------------------------------


def _has_hf_token() -> bool:
    """Module-level helper used by the slow-test skipif."""
    try:
        from helios.keychain import get_hf_token

        return bool(get_hf_token())
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.slow
@pytest.mark.skipif(
    not _has_hf_token(),
    reason="No HF token configured; skipping real pyannote test.",
)
async def test_real_pyannote_pipeline_smoke(
    db, reporter, virtual_clock, enabled_config, tmp_path
):  # pragma: no cover - heavyweight
    """Run a real pyannote pipeline against a synthesized two-speaker fixture.

    Marked ``slow`` and skipped unless a Hugging Face token is in the
    keychain. Not run by default in CI.
    """
    pytest.importorskip("pyannote.audio")

    session_id, _ = await _seed_session_with_chunks(db, tmp_path, n_chunks=4)
    worker = DiarizationWorker(db, enabled_config, virtual_clock, reporter)
    await worker.start()
    await worker.enqueue_session(session_id)
    status = await _wait_for_diarization_status(
        db, session_id, "complete", timeout=120.0
    )
    await worker.stop()
    assert status == "complete"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_chunk_row(
    chunk_id: int,
    session_id: int,
    channel: str,
    start_ts: float,
    end_ts: float,
    path: str,
):
    """Build an AudioChunkRow with the minimum fields ``_concat_wavs_blocking`` reads."""
    from helios.db.rows import AudioChunkRow

    return AudioChunkRow(
        id=chunk_id,
        session_id=session_id,
        channel=channel,
        start_ts=start_ts,
        end_ts=end_ts,
        path=path,
        samples=int((end_ts - start_ts) * 16000),
        partial=False,
        status="recorded",
    )
