"""60-second self-test orchestrator (HELIOS_BUILD_PLAN §6D.2).

Runs the smoke flow on demand:

1. Start a capture session (kind=``continuous``; HELIOS.md §16's CHECK
   constraint doesn't allow a dedicated ``self_test`` kind so we reuse
   ``continuous`` and rely on the session deletion at the end to keep
   the row out of any user-facing list).
2. Sleep ``capture_seconds`` so the chunker produces at least one
   30-second chunk per channel.
3. Stop the session.
4. Verify chunks exist and at least one is non-silent (samples > 0).
5. Trigger a synchronous transcription pass over those chunks via
   ``TranscriptionWorker.transcribe_synchronously`` (when the model is
   ready) and verify segments produced.
6. If diarization is enabled and a worker is wired, enqueue the
   session and wait briefly for ``diarization_status`` to advance.
7. Always — pass or fail — call ``DELETE`` semantics on the session
   (orchestrator stop, trash WAVs, drop the row + cascades).

Returns a structured :class:`SelfTestResult` with a step-level pass/fail
list so the dashboard can render the breakdown directly.

This module never logs raw transcript text. Only chunk counts, segment
counts, and per-step booleans are emitted.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from helios.db import queries
from helios.log import get_logger
from helios.workers.cleanup import trash_session_audio

_log = get_logger("workers.self_test")


@dataclass
class SelfTestStep:
    """One step in the self-test result."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class SelfTestResult:
    """Final state of a self-test run."""

    job_id: str
    status: str = "queued"  # queued / running / complete / failed
    started_at: float | None = None
    finished_at: float | None = None
    steps: list[SelfTestStep] = field(default_factory=list)
    session_id: int | None = None


class SelfTestRunner:
    """One-shot runner for the 60s self-test.

    Construct with the daemon's live dependencies; call :meth:`run`
    once. Reuse-safe (independent ``job_id`` per instance) but designed
    to be created on demand by the diagnostics route.
    """

    def __init__(
        self,
        *,
        db: Any,
        orchestrator: Any,
        config: Any,
        storage_root: Path,
        transcription_worker: Any | None = None,
        diarization_worker: Any | None = None,
        capture_seconds: float = 60.0,
        diarization_wait_seconds: float = 5.0,
        transcribe_timeout_seconds: float = 30.0,
        sleep: Any = None,
    ) -> None:
        self._db = db
        self._orch = orchestrator
        self._config = config
        self._storage_root = Path(storage_root).expanduser()
        self._transcription_worker = transcription_worker
        self._diarization_worker = diarization_worker
        self._capture_seconds = float(capture_seconds)
        self._diarization_wait_seconds = float(diarization_wait_seconds)
        self._transcribe_timeout_seconds = float(transcribe_timeout_seconds)
        # Injectable sleep so tests can fast-forward.
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self.result = SelfTestResult(job_id=f"selftest-{uuid.uuid4().hex[:12]}")

    # ------------------------------------------------------------------

    async def run(self) -> SelfTestResult:
        """Execute the test. Always returns; never raises."""
        self.result.status = "running"
        self.result.started_at = time.time()
        session_id: int | None = None
        try:
            # 1) Start a session.
            session_id = await self._step_start_session()

            # 2-4) Capture, stop, verify chunks.
            if session_id is not None:
                await self._step_capture(session_id)
                await self._step_stop_session(session_id)
                await self._step_verify_chunks(session_id)

                # 5) Transcribe.
                await self._step_transcribe(session_id)

                # 6) Diarize (best-effort).
                await self._step_diarize(session_id)

            # Final status reflects whether any step failed.
            all_ok = all(step.ok for step in self.result.steps)
            self.result.status = "complete" if all_ok else "failed"
        except Exception as exc:  # noqa: BLE001 - never bubble
            _log.warning(
                "self_test_aborted",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            self.result.steps.append(
                SelfTestStep(
                    name="unexpected_error", ok=False, detail=str(exc)
                )
            )
            self.result.status = "failed"
        finally:
            # 7) Always tear down the session.
            if session_id is not None:
                try:
                    await self._cleanup_session(session_id)
                except Exception as exc:  # noqa: BLE001 - never bubble
                    _log.warning(
                        "self_test_cleanup_failed",
                        session_id=session_id,
                        error=str(exc),
                    )
            self.result.finished_at = time.time()
            self.result.session_id = session_id
        return self.result

    # ------------------------------------------------------------------

    async def _step_start_session(self) -> int | None:
        try:
            sid = await self._orch.start_session("continuous")
        except Exception as exc:  # noqa: BLE001
            self.result.steps.append(
                SelfTestStep(
                    name="start_session", ok=False, detail=str(exc)
                )
            )
            return None
        self.result.steps.append(
            SelfTestStep(
                name="start_session",
                ok=True,
                detail=f"session_id={sid}",
            )
        )
        self.result.session_id = sid
        return sid

    async def _step_capture(self, session_id: int) -> None:
        try:
            await self._sleep(self._capture_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.result.steps.append(
                SelfTestStep(
                    name="capture_window", ok=False, detail=str(exc)
                )
            )
            return
        self.result.steps.append(
            SelfTestStep(
                name="capture_window",
                ok=True,
                detail=f"{self._capture_seconds:.0f}s",
            )
        )

    async def _step_stop_session(self, session_id: int) -> None:
        try:
            await self._orch.stop_session(session_id, reason="self_test")
        except Exception as exc:  # noqa: BLE001
            self.result.steps.append(
                SelfTestStep(
                    name="stop_session", ok=False, detail=str(exc)
                )
            )
            return
        self.result.steps.append(
            SelfTestStep(name="stop_session", ok=True)
        )

    async def _step_verify_chunks(self, session_id: int) -> None:
        chunks = await queries.get_session_audio_chunks(
            self._db, session_id
        )
        if not chunks:
            self.result.steps.append(
                SelfTestStep(
                    name="verify_chunks",
                    ok=False,
                    detail="no chunks recorded",
                )
            )
            return
        # ``samples > 0`` is the "non-silent" floor — a chunker that
        # ran with an unavailable source writes ``samples=0`` rows.
        non_silent = [c for c in chunks if int(c.samples or 0) > 0]
        ok = bool(non_silent)
        detail = f"{len(chunks)} chunks, {len(non_silent)} non-silent"
        self.result.steps.append(
            SelfTestStep(name="verify_chunks", ok=ok, detail=detail)
        )

    async def _step_transcribe(self, session_id: int) -> None:
        worker = self._transcription_worker
        if worker is None:
            self.result.steps.append(
                SelfTestStep(
                    name="transcribe",
                    ok=False,
                    detail="transcription worker not wired",
                )
            )
            return
        chunks = await queries.get_session_audio_chunks(
            self._db, session_id
        )
        transcribable = [
            c
            for c in chunks
            if c.path and c.status == "recorded" and int(c.samples or 0) > 0
        ]
        if not transcribable:
            self.result.steps.append(
                SelfTestStep(
                    name="transcribe",
                    ok=False,
                    detail="no transcribable chunks",
                )
            )
            return
        try:
            tx_result = await asyncio.wait_for(
                worker.transcribe_synchronously(transcribable),
                timeout=self._transcribe_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self.result.steps.append(
                SelfTestStep(
                    name="transcribe",
                    ok=False,
                    detail=(
                        f"timed out after {self._transcribe_timeout_seconds:.0f}s "
                        "(model may still be loading)"
                    ),
                )
            )
            return
        except Exception as exc:  # noqa: BLE001
            self.result.steps.append(
                SelfTestStep(
                    name="transcribe",
                    ok=False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            return
        # ``transcribe_synchronously`` returns either a TranscriptResult
        # with ``segments`` or a plain list. Cover both.
        seg_count = 0
        segments = getattr(tx_result, "segments", tx_result)
        try:
            seg_count = len(segments)
        except TypeError:
            seg_count = 0
        self.result.steps.append(
            SelfTestStep(
                name="transcribe",
                ok=seg_count > 0,
                detail=f"{seg_count} segments produced",
            )
        )

    async def _step_diarize(self, session_id: int) -> None:
        # Skip silently when diarization is disabled — the spec says
        # "if enabled". Surface as an ``ok=True`` step with a "skipped"
        # detail so the dashboard can show it dimmed but green.
        enabled = bool(
            getattr(
                getattr(self._config, "diarization", None),
                "enabled",
                False,
            )
        )
        if not enabled or self._diarization_worker is None:
            self.result.steps.append(
                SelfTestStep(
                    name="diarize",
                    ok=True,
                    detail="diarization disabled (skipped)",
                )
            )
            return
        try:
            await self._diarization_worker.enqueue_session(session_id)
        except Exception as exc:  # noqa: BLE001
            self.result.steps.append(
                SelfTestStep(
                    name="diarize",
                    ok=False,
                    detail=f"enqueue failed: {exc}",
                )
            )
            return
        # Poll for status advance, capped at ``_diarization_wait_seconds``.
        deadline = self._diarization_wait_seconds
        elapsed = 0.0
        poll_step = 0.5
        final_status = "pending"
        while elapsed < deadline:
            row = await queries.get_session_by_id(self._db, session_id)
            if row is not None and row.diarization_status in (
                "complete",
                "not_applicable",
                "failed",
            ):
                final_status = row.diarization_status
                break
            await self._sleep(poll_step)
            elapsed += poll_step
        ok = final_status in ("complete", "not_applicable")
        self.result.steps.append(
            SelfTestStep(
                name="diarize",
                ok=ok,
                detail=f"final status={final_status}",
            )
        )

    async def _cleanup_session(self, session_id: int) -> None:
        """Trash WAVs + drop the row + cascades.

        Mirrors the ``DELETE /v1/sessions/{id}`` path so artifact
        lifecycle stays consistent with user-initiated deletion.
        """
        # If the session is still the orchestrator's active session
        # (we got here from an error path), stop it first.
        try:
            if self._orch.active_session_id == session_id:
                await self._orch.stop_session(session_id, reason="self_test_cleanup")
        except Exception as exc:  # noqa: BLE001 - best effort
            _log.warning(
                "self_test_cleanup_stop_failed",
                session_id=session_id,
                error=str(exc),
            )
        try:
            await trash_session_audio(
                db=self._db,
                session_id=session_id,
                storage_root=self._storage_root,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "self_test_cleanup_trash_failed",
                session_id=session_id,
                error=str(exc),
            )
        try:
            await queries.delete_session(self._db, session_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "self_test_cleanup_delete_failed",
                session_id=session_id,
                error=str(exc),
            )


__all__ = [
    "SelfTestResult",
    "SelfTestRunner",
    "SelfTestStep",
]
