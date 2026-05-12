"""Transcription worker — reads pending audio_chunks, writes transcript_segments.

Per HELIOS.md §9.2:
- Loops every ``poll_seconds``, picks up chunks with ``status='recorded'``
  AND ``transcribed_at IS NULL``, calls WhisperX, writes segments to DB.
- mic chunks → ``speaker='user'`` (immediately, no diarization needed).
- system chunks → ``speaker=NULL`` (filled in by merge worker after diarization).
- Up to ``transcription_max_attempts`` retries per chunk; final failure marks
  the chunk ``status='transcription_failed'``.
- Model loads in a background thread on start so the daemon serves
  ``/v1/health`` immediately even on a 60s cold-load. ``os.nice(10)`` is
  applied on the load thread (per spec).

Per HELIOS.md §9.8 — synchronous path for voice notes:
- ``transcribe_synchronously(chunks)`` bypasses the queue, runs immediately,
  returns segments. Pauses the periodic loop so the two paths don't fight
  for the model. Used by ``POST /v1/voice-note/stop``. If the periodic
  worker has already processed a chunk in the input list (e.g. because the
  voice note overlapped a parent meeting), the existing segments are
  reused — no double-transcription. This makes excerpt-mode safe.

Logging contract: never log raw transcript text. Only chunk_id, session_id,
segment count, and durations are emitted. The structlog ``_filter_sensitive``
processor in :mod:`helios.log` redacts ``text``/``transcript_text`` keys as
a defense-in-depth measure even if a caller slips up.
"""

from __future__ import annotations

import asyncio
import json
import os
import wave
from dataclasses import dataclass
from typing import Any

import aiosqlite
import numpy as np

from helios.clock import Clock
from helios.components import ComponentStatusReporter
from helios.config import HeliosConfig
from helios.db import queries
from helios.db.rows import AudioChunkRow
from helios.log import get_logger

_log = get_logger("workers.transcription")


@dataclass
class TranscribedSegment:
    """One Whisper segment ready to persist as a ``transcript_segments`` row."""

    chunk_id: int
    segment_index: int
    start_ts: float  # epoch seconds (chunk.start_ts + segment.start)
    end_ts: float
    text: str  # raw transcribed text — DO NOT LOG
    speaker: str | None  # 'user' for mic chunks, None for system (until merge)
    words: list[dict] | None = None  # word-level timestamps if word_timestamps=True


@dataclass
class TranscriptResult:
    """Returned by :meth:`TranscriptionWorker.transcribe_synchronously`."""

    segments: list[TranscribedSegment]


class TranscriptionWorker:
    """Background worker that drains ``audio_chunks`` → ``transcript_segments``.

    Construction does not load the WhisperX model. ``start()`` spawns a
    background task that imports + loads the model so the daemon's
    ``/v1/health`` endpoint stays responsive during the 30-60s cold load.
    Until the model is ready, both the periodic loop and any
    ``transcribe_synchronously()`` callers wait on ``_model_ready``.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        config: HeliosConfig,
        clock: Clock,
        reporter: ComponentStatusReporter,
        poll_seconds: float = 5.0,
    ) -> None:
        self._db = db
        self._config = config
        self._clock = clock
        self._reporter = reporter
        self._poll_seconds = poll_seconds

        self._model: Any | None = None
        self._model_ready: asyncio.Event = asyncio.Event()
        # Word-level alignment is optional (config.transcription.word_timestamps).
        # Loaded after the main model in the same background task — failure to
        # load is non-fatal: transcription continues without per-word stamps.
        self._align_model: Any | None = None
        self._align_metadata: dict | None = None
        self._loop_task: asyncio.Task | None = None
        self._load_task: asyncio.Task | None = None
        self._stopping = False
        self._pause_normal_loop = False
        # Serialize transcribe_synchronously() so two voice notes don't race
        # for the same model instance.
        self._sync_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------ public

    async def start(self) -> None:
        """Spawn the background model-load + the polling loop. Idempotent."""
        if self._loop_task is not None and not self._loop_task.done():
            return
        # Kick off the model load in a background thread so daemon startup
        # is not blocked by a 60s cold-load.
        await self._reporter.set(
            "transcription",
            "unavailable",
            reason="model_loading",
            action="WhisperX model loading in background",
        )
        self._stopping = False
        self._model_ready.clear()
        self._load_task = asyncio.create_task(
            self._load_model_background(), name="transcription-load"
        )
        self._loop_task = asyncio.create_task(
            self._run_loop(), name="transcription-worker"
        )

    async def stop(self) -> None:
        """Cancel both background tasks. Safe to call multiple times."""
        self._stopping = True
        for task in (self._load_task, self._loop_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    # Either the cancel propagated or the task raised before
                    # the cancel landed — both are acceptable shutdown paths.
                    pass
        self._load_task = None
        self._loop_task = None

    async def transcribe_synchronously(
        self, chunks: list[AudioChunkRow]
    ) -> TranscriptResult:
        """Run transcription on a specific list of chunks immediately.

        Used by ``POST /v1/voice-note/stop``. Pauses the normal poll loop
        while running so the model isn't fighting itself. Reuses already-
        transcribed chunks (from the periodic worker) to avoid double work.

        Excerpt mode: when the voice note overlaps a parent meeting session,
        the parent's mic chunks may already have been picked up by the
        periodic loop. We detect that via ``chunk.transcribed_at is not None``
        and re-fetch the existing segments instead of re-running WhisperX.

        Serialized via ``_sync_lock`` so two callers cannot race for the
        same model.
        """
        async with self._sync_lock:
            await self._model_ready.wait()
            self._pause_normal_loop = True
            try:
                results: list[TranscribedSegment] = []
                for chunk in chunks:
                    if chunk.transcribed_at is not None:
                        # Already transcribed by the normal loop — fetch
                        # existing segments and reuse them.
                        existing = await queries.get_segments_for_chunk(
                            self._db, chunk.id
                        )
                        for s in existing:
                            results.append(
                                TranscribedSegment(
                                    chunk_id=chunk.id,
                                    segment_index=s.segment_index,
                                    start_ts=s.start_ts,
                                    end_ts=s.end_ts,
                                    text=s.text,
                                    speaker=s.speaker,
                                    words=None,
                                )
                            )
                        continue
                    segments = await self._transcribe_chunk(chunk)
                    if segments:
                        for seg in segments:
                            await queries.insert_transcript_segment(
                                self._db,
                                **_segment_to_row_kwargs(seg),
                            )
                        await queries.mark_chunk_transcribed(
                            self._db, chunk.id
                        )
                        results.extend(segments)
                    else:
                        # Chunk produced no segments (silent / no speech).
                        # Mark transcribed to keep the queue moving — leaving
                        # it pending would have the periodic worker retry
                        # forever against an empty audio file.
                        await queries.mark_chunk_transcribed(
                            self._db, chunk.id
                        )
                return TranscriptResult(segments=results)
            finally:
                self._pause_normal_loop = False

    # ------------------------------------------------------------------ internals

    async def _load_model_background(self) -> None:
        """Load the WhisperX model on a background thread.

        Reports component status:

        * ``unavailable(reason=model_loading)`` immediately on entry.
        * ``unavailable(reason=whisperx_not_installed)`` if the lazy import
          fails — the operator just hasn't installed the optional extras.
        * ``error(reason=model_load_timeout|model_load_failed)`` on hard
          failure (the state machine flips into ``error`` for these).
        * ``ok`` on success.
        """
        loop = asyncio.get_running_loop()
        try:
            try:
                import whisperx  # type: ignore[import-not-found]
            except ImportError as exc:
                await self._reporter.set(
                    "transcription",
                    "unavailable",
                    reason="whisperx_not_installed",
                    detail=str(exc),
                    action="Install with: uv pip install '.[transcription]'",
                )
                return

            cfg = self._config.transcription
            timeout = float(cfg.model_load_timeout_seconds)
            try:
                model = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        self._load_model_blocking,
                        whisperx,
                        cfg.model,
                        cfg.language,
                        cfg.compute_type,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                await self._reporter.set(
                    "transcription",
                    "error",
                    reason="model_load_timeout",
                    detail=f"Model did not load within {timeout}s",
                    action=(
                        f"Run: python -m helios.scripts.download_whisper "
                        f"--model {cfg.model}"
                    ),
                )
                return

            self._model = model
            self._model_ready.set()
            await self._reporter.set("transcription", "ok")
            _log.info(
                "transcription_model_loaded",
                model=cfg.model,
                language=cfg.language,
                compute_type=cfg.compute_type,
            )

            # Best-effort word-level alignment model (WhisperX 3.x splits
            # word timestamps into a separate alignment step). Failure
            # here is non-fatal — transcription proceeds without
            # per-word stamps, which is what callers used to get on the
            # 3.8 transcribe path before this. We load AFTER setting the
            # main model ready so the periodic loop doesn't block on us.
            if cfg.word_timestamps:
                try:
                    align_model, align_meta = await loop.run_in_executor(
                        None,
                        whisperx.load_align_model,
                        cfg.language,
                        "cpu",
                    )
                    self._align_model = align_model
                    self._align_metadata = align_meta
                    _log.info(
                        "transcription_align_model_loaded",
                        language=cfg.language,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "transcription_align_model_load_failed",
                        language=cfg.language,
                        error=str(exc),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._reporter.set(
                "transcription",
                "error",
                reason="model_load_failed",
                detail=str(exc),
                action="Check logs; restart daemon to retry.",
            )
            _log.error("transcription_model_load_failed", error=str(exc))

    def _load_model_blocking(
        self,
        whisperx: Any,
        model_name: str,
        language: str,
        compute_type: str,
    ) -> Any:
        """Run on the executor thread. Lower priority + load + return model."""
        try:
            os.nice(self._config.transcription.worker_nice)
        except (AttributeError, PermissionError, OSError):
            # ``os.nice`` is unavailable on Windows / containers / when the
            # niceness change is rejected. Not fatal — proceed at default
            # priority.
            pass
        # Apple Silicon: device='mps' would be ideal but compatibility with
        # WhisperX is patchy. Fall back to 'cpu' with int8/float16
        # compute_type per config (the user's machine will pick the right
        # one for their hardware via the config file).
        return whisperx.load_model(
            model_name,
            device="cpu",
            compute_type=compute_type,
            language=language,
            vad_method=self._config.transcription.vad_method,
        )

    async def _run_loop(self) -> None:
        """Poll for pending chunks; transcribe; sleep; repeat."""
        await self._model_ready.wait()
        try:
            while not self._stopping:
                if self._pause_normal_loop:
                    await self._clock.sleep(0.1)
                    continue
                try:
                    chunks = await queries.get_pending_chunks(
                        self._db, limit=20
                    )
                except Exception as exc:  # noqa: BLE001
                    _log.warning("pending_chunks_fetch_failed", error=str(exc))
                    chunks = []
                for chunk in chunks:
                    if self._stopping or self._pause_normal_loop:
                        break
                    await self._process_chunk(chunk)
                if self._stopping:
                    return
                await self._clock.sleep(self._poll_seconds)
        except asyncio.CancelledError:
            raise

    async def _process_chunk(self, chunk: AudioChunkRow) -> None:
        """Transcribe one chunk; persist segments; mark chunk transcribed.

        Retries up to ``transcription_max_attempts`` times; on final failure
        marks ``status='transcription_failed'`` and bumps ``attempts``.
        Re-raises ``asyncio.CancelledError`` so worker shutdown is fast.
        """
        max_attempts = self._config.transcription.transcription_max_attempts
        attempts = chunk.transcription_attempts or 0
        try:
            segments = await self._transcribe_chunk(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            attempts += 1
            if attempts >= max_attempts:
                await queries.mark_chunk_transcription_failed(
                    self._db, chunk.id, attempts=attempts
                )
                _log.warning(
                    "chunk_transcription_failed",
                    chunk_id=chunk.id,
                    attempts=attempts,
                    error=str(exc),
                )
            else:
                # Bump attempts so the next loop iteration sees the higher
                # count without changing status (still 'recorded').
                await queries.update_chunk_transcription_attempts(
                    self._db, chunk.id, attempts=attempts
                )
                _log.info(
                    "chunk_transcription_retry",
                    chunk_id=chunk.id,
                    attempts=attempts,
                    error=str(exc),
                )
            return
        for seg in segments:
            await queries.insert_transcript_segment(
                self._db, **_segment_to_row_kwargs(seg)
            )
        await queries.mark_chunk_transcribed(self._db, chunk.id)
        _log.info(
            "chunk_transcription_completed",
            chunk_id=chunk.id,
            session_id=chunk.session_id,
            segment_count=len(segments),
            duration_seconds=round(chunk.end_ts - chunk.start_ts, 3),
        )

    async def _transcribe_chunk(
        self, chunk: AudioChunkRow
    ) -> list[TranscribedSegment]:
        """Run WhisperX on the chunk's WAV file; return segments.

        Returns an empty list if the chunk has no on-disk path (already
        archived) or if the model produced no recognised speech.
        """
        if self._model is None or chunk.path is None:
            return []
        loop = asyncio.get_running_loop()
        cfg = self._config.transcription
        # Decode the WAV ourselves with stdlib + numpy so WhisperX's
        # ``transcribe`` / ``align`` skip their internal ``load_audio``
        # branch — that branch shells out to ``ffmpeg``, which is not
        # bundled in Helios.app and is not on the LaunchAgent's PATH.
        # Our capture pipeline writes 16-bit mono PCM at the configured
        # sample rate (see ``capture.chunker._write_wav_blocking``),
        # which matches Whisper's expected input.
        audio = await loop.run_in_executor(None, _load_wav_mono_16k, chunk.path)
        result = await loop.run_in_executor(
            None,
            self._call_model_transcribe,
            audio,
            cfg.language,
            cfg.vad_filter,
            cfg.word_timestamps,
        )
        # WhisperX returns a dict ``{"segments": [...]}``; older / faster-
        # whisper-style backends return a tuple ``(segments, info)``.
        # Accept both. Each segment has ``{start, end, text, words?}``.
        if isinstance(result, dict):
            segs = result.get("segments", []) or []
        elif isinstance(result, tuple) and len(result) >= 1:
            segs = list(result[0]) if result[0] is not None else []
        else:  # pragma: no cover - defensive
            segs = list(result) if result is not None else []

        speaker = "user" if chunk.channel == "mic" else None
        out: list[TranscribedSegment] = []
        for idx, seg in enumerate(segs):
            text = _seg_field(seg, "text", "").strip()
            if not text:
                continue
            rel_start = float(_seg_field(seg, "start", 0.0))
            rel_end = float(_seg_field(seg, "end", 0.0))
            words = _normalise_words(
                _seg_field(seg, "words", None), chunk.start_ts
            )
            out.append(
                TranscribedSegment(
                    chunk_id=chunk.id,
                    segment_index=idx,
                    start_ts=chunk.start_ts + rel_start,
                    end_ts=chunk.start_ts + rel_end,
                    text=text,
                    speaker=speaker,
                    words=words,
                )
            )
        return out

    def _call_model_transcribe(
        self,
        audio: np.ndarray,
        language: str,
        vad_filter: bool,
        word_timestamps: bool,
    ) -> Any:
        """Invoke ``self._model.transcribe(...)`` from the executor thread.

        WhisperX 3.8+'s ``FasterWhisperPipeline.transcribe()`` signature is
        ``(audio, batch_size=, num_workers=, language=, task=, chunk_size=,
        print_progress=, combined_progress=, verbose=)``. VAD is configured
        at model-load time (``whisperx.load_model(..., vad_method=...)``);
        word-level timestamps come from a separate alignment step
        (``whisperx.align``). The legacy ``faster_whisper`` kwargs
        ``vad_filter`` / ``word_timestamps`` are NOT accepted directly by
        ``transcribe()``. ``vad_filter`` belongs at model-load time;
        ``word_timestamps`` is honored here by running ``whisperx.align``
        after transcribe when the alignment model is loaded.

        ``audio`` must be a 1-D float32 numpy array at 16 kHz mono — see
        ``_transcribe_chunk`` for why we pre-decode rather than pass a
        path string.
        """
        del vad_filter  # see docstring; configured at model-load time
        assert self._model is not None  # noqa: S101 - invariant
        result = self._model.transcribe(
            audio,
            language=language,
            batch_size=8,
        )
        # Word-level alignment — best-effort. Skip when disabled, when
        # the alignment model failed to load, or when transcribe produced
        # no segments. Any failure here logs and returns the un-aligned
        # result so segment-level transcription is preserved.
        if (
            word_timestamps
            and self._align_model is not None
            and self._align_metadata is not None
        ):
            try:
                segments = result.get("segments") if isinstance(result, dict) else None
                if segments:
                    import whisperx  # type: ignore[import-not-found]

                    aligned = whisperx.align(
                        segments,
                        self._align_model,
                        self._align_metadata,
                        audio,
                        device="cpu",
                        return_char_alignments=False,
                    )
                    # ``align`` returns ``{"segments": [...], "word_segments": [...]}``.
                    # Replace segments with the aligned ones; downstream code
                    # already extracts ``words`` per segment.
                    result["segments"] = aligned.get("segments", segments)
            except Exception as exc:  # noqa: BLE001 — never break transcription on align
                _log.warning(
                    "transcription_align_failed",
                    error=str(exc),
                )
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_WHISPER_SAMPLE_RATE = 16_000


def _load_wav_mono_16k(path: str) -> np.ndarray:
    """Decode a 16-bit mono PCM WAV at ``_WHISPER_SAMPLE_RATE`` to float32.

    Why this exists: WhisperX's ``transcribe`` / ``align`` call
    ``whisperx.audio.load_audio`` when given a path, which subprocess-execs
    ``ffmpeg``. The Helios.app bundle does not ship ffmpeg, and the
    LaunchAgent's PATH excludes Homebrew, so every transcription attempt
    failed with ``[Errno 2] No such file or directory: 'ffmpeg'``. The
    capture pipeline always writes 16-bit mono PCM (see
    ``capture.chunker._write_wav_blocking``), so a stdlib ``wave`` reader
    + numpy conversion suffices and removes the dependency entirely.
    """
    with wave.open(path, "rb") as f:
        sample_rate = f.getframerate()
        channels = f.getnchannels()
        sampwidth = f.getsampwidth()
        frames = f.readframes(f.getnframes())
    if sampwidth != 2 or channels != 1 or sample_rate != _WHISPER_SAMPLE_RATE:
        raise ValueError(
            f"unexpected WAV format at {path}: sampwidth={sampwidth} "
            f"channels={channels} sample_rate={sample_rate}; "
            f"expected 16-bit mono {_WHISPER_SAMPLE_RATE} Hz"
        )
    pcm = np.frombuffer(frames, dtype=np.int16)
    return (pcm.astype(np.float32) / 32768.0).copy()


def _seg_field(seg: Any, name: str, default: Any) -> Any:
    """Read a field from a Whisper segment, supporting dict + attribute access.

    WhisperX returns dicts; faster-whisper returns objects with attributes;
    pyannote / replays produce dicts. We accept all of them.
    """
    if isinstance(seg, dict):
        return seg.get(name, default)
    return getattr(seg, name, default)


def _normalise_words(words: Any, base_ts: float) -> list[dict] | None:
    """Convert WhisperX word objects to plain dicts with absolute timestamps.

    Returns ``None`` when the segment has no per-word timestamps. Each word
    dict has ``{"word", "start", "end", "probability"}`` with timestamps
    shifted from chunk-relative to epoch absolute.
    """
    if not words:
        return None
    out: list[dict] = []
    for w in words:
        word_text = _seg_field(w, "word", "")
        rel_start = _seg_field(w, "start", None)
        rel_end = _seg_field(w, "end", None)
        prob = _seg_field(w, "probability", None)
        # WhisperX sometimes emits None timestamps for very short tokens;
        # skip those rather than emit garbled rows.
        if rel_start is None or rel_end is None:
            continue
        out.append(
            {
                "word": word_text,
                "start": base_ts + float(rel_start),
                "end": base_ts + float(rel_end),
                "probability": float(prob) if prob is not None else None,
            }
        )
    return out or None


def _segment_to_row_kwargs(s: TranscribedSegment) -> dict:
    """Map a :class:`TranscribedSegment` to ``insert_transcript_segment`` kwargs.

    The migration's ``transcript_segments`` table stores per-word data as a
    JSON-encoded TEXT column named ``words``; we serialise here so callers
    don't need to remember.
    """
    return {
        "chunk_id": s.chunk_id,
        "segment_index": s.segment_index,
        "start_ts": s.start_ts,
        "end_ts": s.end_ts,
        "text": s.text,
        "speaker": s.speaker,
        "words": None if s.words is None else json.dumps(s.words),
    }
