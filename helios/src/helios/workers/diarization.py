"""Diarization worker — runs pyannote on session audio, writes diarization_turns.

Per HELIOS.md §9.3:

* Triggered when a session ends. The orchestrator (or scheduler) calls
  :meth:`DiarizationWorker.enqueue_session` once a session's recording
  cycle has finished.
* The worker concatenates the session's ``system``-channel WAV files into
  a single temp file, runs ``pyannote/speaker-diarization-3.1`` against
  it, and writes one ``diarization_turns`` row per (speaker, span)
  pyannote produces. Wave 3I's merge worker later joins these turns to
  ``transcript_segments`` by time-overlap to assign speaker labels.
* Diarization is *skippable*. A daemon without pyannote installed, or
  without a Hugging Face access token, must still run cleanly — system
  segments simply stay ``speaker=NULL`` until the user re-enables. The
  ``diarization`` component reports the appropriate ``unavailable``
  reason so the API surfaces a remediation hint.

Component status reasons (HELIOS.md §9.7):

* ``unavailable(reason="disabled")``               — DiarizationConfig.enabled=False
* ``unavailable(reason="token_missing")``          — no HF token in keychain
* ``unavailable(reason="model_loading")``          — initial load in progress
* ``unavailable(reason="pyannote_not_installed")`` — pyannote.audio import failed
* ``error(reason="model_load_failed")``            — Pipeline.from_pretrained raised
* ``ok``                                           — pipeline loaded, ready to consume queue
"""

from __future__ import annotations

import asyncio
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import numpy as np

from helios.clock import Clock
from helios.components import ComponentStatusReporter
from helios.config import HeliosConfig
from helios.db import queries
from helios.db.rows import AudioChunkRow
from helios.keychain import get_hf_token
from helios.log import get_logger

_log = get_logger("workers.diarization")

# pyannote.audio 4.0 deprecated speaker-diarization-3.1 in favor of
# speaker-diarization-community-1, and 4.x routes 3.1 internally to
# community-1 anyway. Both are gated — the user must accept terms at
# https://hf.co/pyannote/speaker-diarization-community-1 with their HF
# account before their token will succeed here.
_PYANNOTE_MODEL_ID = "pyannote/speaker-diarization-community-1"


@dataclass
class DiarizationTurn:
    """One contiguous span attributed to a single speaker.

    Mirrors a row inserted into ``diarization_turns``. ``start_ts`` and
    ``end_ts`` are absolute UTC epoch seconds (offset added on top of
    pyannote's session-relative seconds).
    """

    session_id: int
    speaker_label: str       # pyannote-assigned (e.g., 'SPEAKER_00')
    start_ts: float
    end_ts: float
    embedding: bytes | None  # float32 BLOB if config.store_embeddings else None


class DiarizationWorker:
    """Background worker that diarizes ended sessions.

    Lifecycle:

    1. :meth:`start` — reports ``unavailable`` if disabled or token
       missing, else spawns a background pipeline-load task and a
       queue-consumer loop. Idempotent.
    2. Once :meth:`enqueue_session` has been called and the pipeline is
       ready, the consumer loop pulls each session id, concatenates its
       system-channel WAVs into a temp file, runs pyannote, and writes
       ``diarization_turns`` rows.
    3. :meth:`stop` — cancels both tasks. Idempotent.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        config: HeliosConfig,
        clock: Clock,
        reporter: ComponentStatusReporter,
    ) -> None:
        self._db = db
        self._config = config
        self._clock = clock
        self._reporter = reporter

        self._pipeline: Any | None = None
        self._pipeline_ready = asyncio.Event()
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._loop_task: asyncio.Task | None = None
        self._load_task: asyncio.Task | None = None
        self._stopping = False
        self._enabled: bool = bool(self._config.diarization.enabled)

        # Wave 3I: merge worker reference. Wired via
        # :meth:`set_merge_worker` after construction. After diarization
        # successfully writes turns for a session, the worker enqueues
        # the session id to the merge worker which assigns
        # ``transcript_segments.speaker`` for system-channel segments.
        # ``None`` ⇒ no merge worker wired (e.g. tests).
        self._merge_worker = None  # type: ignore[var-annotated]

    # ------------------------------------------------------------------ public

    async def start(self) -> None:
        """Spawn the background pipeline-load + the queue-consumer loop.

        Idempotent — repeat calls while a loop is already running are
        no-ops. If diarization is disabled or no HF token is configured,
        the appropriate ``unavailable`` reason is reported and no tasks
        are spawned (callers can still ``enqueue_session`` but nothing
        will be drained until the worker is reconfigured + restarted).
        """
        if self._loop_task is not None and not self._loop_task.done():
            return

        if not self._enabled:
            await self._reporter.set(
                "diarization",
                "unavailable",
                reason="disabled",
                detail="diarization.enabled is False in config",
            )
            return

        token = get_hf_token()
        if token is None:
            await self._reporter.set(
                "diarization",
                "unavailable",
                reason="token_missing",
                action="Run: python -m helios.scripts.set_hf_token",
            )
            return

        # We're going to attempt to load. Surface the loading state so
        # callers polling /v1/status see a transient signal rather than
        # a stale ``ok`` from a previous run.
        await self._reporter.set(
            "diarization",
            "unavailable",
            reason="model_loading",
        )

        self._stopping = False
        self._load_task = asyncio.create_task(
            self._load_pipeline_background(token), name="diarization-load"
        )
        self._loop_task = asyncio.create_task(
            self._run_loop(), name="diarization-worker"
        )

    async def stop(self) -> None:
        """Cancel the background tasks. Idempotent."""
        self._stopping = True
        for task in (self._load_task, self._loop_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._load_task = None
        self._loop_task = None

    async def enqueue_session(self, session_id: int) -> None:
        """Mark a session as ready to diarize.

        Called by the orchestrator (Wave 3I/3J integration) at session
        end. Even if the worker isn't running yet, the queue accepts the
        id — once :meth:`start` succeeds the loop will drain pending
        items.
        """
        await self._queue.put(session_id)

    def set_merge_worker(self, worker) -> None:
        """Wire the merge worker reference (Wave 3I).

        Called once at lifespan setup. After ``_diarize_session``
        successfully writes ``diarization_turns`` rows for a session,
        the diarization worker enqueues that session id to the merge
        worker. ``None`` ⇒ no merge step (segments stay
        ``speaker=NULL``).
        """
        self._merge_worker = worker

    # ----------------------------------------------------------------- internal

    async def _load_pipeline_background(self, token: str) -> None:
        """Lazy-import + load the pyannote pipeline off the event loop."""
        loop = asyncio.get_running_loop()
        try:
            try:
                from pyannote.audio import Pipeline  # type: ignore[import-not-found]
            except ImportError as exc:
                await self._reporter.set(
                    "diarization",
                    "unavailable",
                    reason="pyannote_not_installed",
                    detail=str(exc),
                    action="Install with: uv pip install '.[transcription]'",
                )
                return

            try:
                # pyannote.audio 4.0 renamed ``use_auth_token`` → ``token``
                # to align with huggingface_hub. The 3.x kwarg is gone, so
                # passing it raises TypeError. Stick to the 4.x name.
                pipeline = await loop.run_in_executor(
                    None,
                    lambda: Pipeline.from_pretrained(
                        _PYANNOTE_MODEL_ID, token=token
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — Pipeline can raise anything
                await self._reporter.set(
                    "diarization",
                    "error",
                    reason="model_load_failed",
                    detail=str(exc),
                )
                return

            self._pipeline = pipeline
            self._pipeline_ready.set()
            await self._reporter.set("diarization", "ok")
            _log.info("diarization_pipeline_loaded")
        except asyncio.CancelledError:
            raise

    async def _run_loop(self) -> None:
        """Consume queued session ids once the pipeline is ready."""
        # Wait for the pipeline to load OR for stop() to be called. We
        # poll the ready event with a small timeout so a cancellation
        # during model load tears the task down promptly.
        while not self._stopping and not self._pipeline_ready.is_set():
            try:
                await asyncio.wait_for(self._pipeline_ready.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
        if self._stopping:
            return

        try:
            while not self._stopping:
                try:
                    session_id = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                if self._stopping:
                    return
                try:
                    await self._diarize_session(session_id)
                except Exception as exc:  # noqa: BLE001 — never kill the loop
                    _log.warning(
                        "diarization_session_failed",
                        session_id=session_id,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    await queries.update_session_diarization_status(
                        self._db, session_id, status="failed"
                    )
        except asyncio.CancelledError:
            raise

    async def _diarize_session(self, session_id: int) -> None:
        """Diarize one session: concat WAVs, run pipeline, persist turns."""
        # 1) Gather the session's system-channel WAVs.
        chunks = await queries.get_session_audio_chunks(self._db, session_id)
        system_chunks = [
            c for c in chunks
            if c.channel == "system" and c.path and c.status == "recorded"
        ]
        if not system_chunks:
            await queries.update_session_diarization_status(
                self._db, session_id, status="not_applicable"
            )
            _log.info(
                "diarization_skipped_no_system_audio",
                session_id=session_id,
            )
            return

        # 2) Concat to a temp WAV. We delete=False on creation because we
        # need the file to live across the run_in_executor call; an
        # explicit unlink in `finally` guarantees cleanup.
        tmp_handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_handle.close()
        tmp_path = Path(tmp_handle.name)

        try:
            await asyncio.get_running_loop().run_in_executor(
                None, _concat_wavs_blocking, system_chunks, tmp_path
            )
            await queries.update_session_diarization_status(
                self._db, session_id, status="running"
            )

            # 3) Run pyannote off the loop. The pipeline is blocking and
            # GIL-bound; run_in_executor keeps the event loop responsive.
            loop = asyncio.get_running_loop()
            min_speakers = int(self._config.diarization.min_speakers)
            max_speakers = int(self._config.diarization.max_speakers)
            diar_result = await loop.run_in_executor(
                None,
                _run_pipeline_blocking,
                self._pipeline,
                str(tmp_path),
                min_speakers,
                max_speakers,
            )

            # 4) Convert pyannote.Annotation segments to absolute-time
            # DiarizationTurns relative to the session's first chunk.
            base_ts = float(system_chunks[0].start_ts)
            store_embeddings = bool(self._config.diarization.store_embeddings)
            turn_count = 0
            for turn, _track, speaker in diar_result.itertracks(yield_label=True):
                embedding_blob: bytes | None = None
                if store_embeddings:
                    embedding_blob = _extract_embedding_blob(diar_result, speaker)

                await queries.insert_diarization_turn(
                    self._db,
                    session_id=session_id,
                    speaker_label=str(speaker),
                    start_ts=base_ts + float(turn.start),
                    end_ts=base_ts + float(turn.end),
                    embedding=embedding_blob,
                )
                turn_count += 1

            await queries.update_session_diarization_status(
                self._db, session_id, status="complete"
            )
            _log.info(
                "diarization_complete",
                session_id=session_id,
                turns=turn_count,
            )

            # Wave 3I: trigger the merge worker once turns are
            # persisted. Best-effort: enqueue must not crash the
            # diarization loop. The merge worker itself is no-op when
            # there are no turns or the segments aren't yet written.
            if self._merge_worker is not None:
                try:
                    await self._merge_worker.enqueue_session(session_id)
                except Exception as exc:  # pragma: no cover - defensive
                    _log.warning(
                        "merge_enqueue_failed",
                        session_id=session_id,
                        error=str(exc),
                    )
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _run_pipeline_blocking(
    pipeline: Any,
    wav_path: str,
    min_speakers: int,
    max_speakers: int,
) -> Any:
    """Call the pyannote pipeline. Module-level so executors can pickle it."""
    return pipeline(
        wav_path,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
    )


def _extract_embedding_blob(diar_result: Any, speaker: str) -> bytes | None:
    """Best-effort extraction of a per-speaker embedding into a float32 BLOB.

    pyannote 3.1's diarization pipeline can attach a ``speaker_embeddings``
    mapping when configured to do so, but the surface varies by version
    and whether the pipeline was instantiated with ``return_embeddings``.
    We treat absence as "no embedding available" — the DB column is
    nullable for exactly this reason.
    """
    try:
        embeddings = getattr(diar_result, "speaker_embeddings", None)
        if embeddings is None:
            return None
        emb = embeddings.get(speaker) if hasattr(embeddings, "get") else None
        if emb is None:
            return None
        return np.asarray(emb, dtype=np.float32).tobytes()
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _concat_wavs_blocking(chunks: list[AudioChunkRow], out_path: Path) -> None:
    """Concatenate WAV files with matching sample rate / channels / bit depth.

    The first existing file's format is used as the canonical format; any
    chunk with a missing path is skipped (it may have been archived or
    never written). Raises ``ValueError`` if no usable chunks exist.

    We pre-scan for at least one readable input before opening the output
    so that the error path doesn't leave a partial / header-less WAV on
    disk and doesn't trigger ``wave.Error`` from the writer's close path.
    """
    if not chunks:
        raise ValueError("no chunks to concatenate")

    readable: list[Path] = []
    for c in chunks:
        if not c.path:
            continue
        src_path = Path(c.path)
        if src_path.exists():
            readable.append(src_path)

    if not readable:
        raise ValueError("no readable WAV files in chunks")

    with wave.open(str(out_path), "wb") as out:
        first = True
        for src_path in readable:
            with wave.open(str(src_path), "rb") as src:
                if first:
                    out.setnchannels(src.getnchannels())
                    out.setsampwidth(src.getsampwidth())
                    out.setframerate(src.getframerate())
                    first = False
                out.writeframes(src.readframes(src.getnframes()))
