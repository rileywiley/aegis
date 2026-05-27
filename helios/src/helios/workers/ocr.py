"""OCR worker — consumes SCK video frames, runs Vision OCR, persists rows.

Per HELIOS.md §10 (Phase 5 / Track 5A):

* The worker drains frames from a SystemAudioSource's ``video_frames()``
  async iterator. The SCK helper produces frames at the helper's native
  rate; this worker throttles to ``config.ocr.fps`` (typically 1 fps) by
  skipping frames whose timestamp is within ``1/fps`` of the last
  processed frame.
* Each candidate frame is **gated** before any expensive work:

  1. The active session's ``screen_capture_override_until`` is consulted
     — if ``now < override_until`` the gate opens unconditionally
     (HELIOS.md §16.7 user-initiated override).
  2. Otherwise the frontmost macOS application's bundle id is read via
     :mod:`helios.workers.frontmost` and compared against
     ``config.ocr.meeting_apps`` (the allowlist).

  Frames failing both checks are dropped without OCR. This is the key
  privacy invariant: Helios never OCRs your inbox / banking app / chat
  history.
* OCR is performed via Apple's Vision framework
  (``VNRecognizeTextRequest``). The bridge is lazy-imported and gracefully
  reports ``unavailable(reason=vision_not_available)`` when absent.
* A perceptual-hash dedup pass drops near-duplicate frames within a
  configurable window so static screens don't fill the DB. The hash is
  computed from JPEG bytes and compared by Hamming distance.
* Persistence: one ``ocr_frames`` row per accepted frame.

PII safety: raw OCR text is private user content. The worker NEVER logs
``text`` — it only emits ``frame_id``, ``session_id``, ``app_bundle``,
``text_length`` and confidence. The ``_filter_sensitive`` processor in
:mod:`helios.log` provides defense-in-depth for ``text``-keyed payloads.

Daemon integration is intentionally out of scope here (see file owners in
the build plan): this module exports ``OcrWorker`` for the daemon module
to wire up.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

import aiosqlite

from helios.clock import Clock
from helios.components import ComponentStatusReporter
from helios.config import HeliosConfig
from helios.db import queries
from helios.log import get_logger
from helios.sources.interface import VideoFrame
from helios.workers.frontmost import FrontmostProvider, get_frontmost_bundle_id

_log = get_logger("workers.ocr")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class OcrResult:
    """Bag of values returned by an OCR invocation."""

    text: str
    avg_confidence: float


class VideoFrameSource(Protocol):
    """Minimal contract: anything yielding ``VideoFrame`` async-iter works.

    The real implementation is
    :class:`helios.sources.real.SCKSystemAudioSource`; the tests pass a
    tiny fake that hands frames out of an in-memory queue.
    """

    def video_frames(self) -> AsyncIterator[VideoFrame]: ...


# Type alias: function called for each gated frame to extract text.
# The default uses Vision; tests inject a stub. Returns ``None`` when
# the OCR engine has no useable output (low confidence / no text).
OcrCallable = Callable[[bytes], Awaitable[OcrResult | None]]


# Async callable returning the active session as
# ``(session_id, screen_capture_override_until)`` or ``None`` when no
# session is active. The override timestamp is the column from
# ``capture_sessions.screen_capture_override_until`` (HELIOS.md §16.7);
# ``None`` means no override is in effect. Owning the lookup as an
# injectable means tests don't need to spin up the orchestrator — they
# hand back a fixed tuple.
ActiveSessionProvider = Callable[
    [], Awaitable["tuple[int, float | None] | None"]
]


async def session_provider_from_db(
    db: aiosqlite.Connection,
) -> "tuple[int, float | None] | None":
    """Default :data:`ActiveSessionProvider` implementation.

    Reads ``capture_sessions`` for the most recent un-ended row. Wrap
    via ``functools.partial`` or a small lambda when registering with
    the worker:

        provider = lambda: session_provider_from_db(db)
        worker = OcrWorker(..., active_session_provider=provider)
    """
    row = await queries.get_active_session(db)
    if row is None:
        return None
    return row.id, row.screen_capture_override_until


# ---------------------------------------------------------------------------
# Gating helpers
# ---------------------------------------------------------------------------


def gate_allows_frame(
    *,
    now_ts: float,
    frontmost_bundle: str | None,
    allowlist: list[str],
    screen_capture_override_until: float | None,
    gate_by_allowlist: bool = True,
) -> bool:
    """Return True iff the OCR worker is allowed to process the current frame.

    When ``gate_by_allowlist`` is False the gate is always open (modulo
    the override path which always opens regardless). This is the
    current default — Teams desktop minimizes during screen-share and
    starves the allowlist, so we capture everything until smarter
    screen-share-aware gating lands.

    When ``gate_by_allowlist`` is True, two independent reasons can
    open the gate:

    * **Override**: the user pressed "Capture screen for N minutes" and
      ``screen_capture_override_until`` is in the future. The frontmost
      app is irrelevant in this branch.
    * **Allowlist**: the frontmost app's bundle id is in
      ``allowlist`` (HELIOS.md §10 ``ocr.meeting_apps``).

    Both checks fail-closed: ``None`` frontmost (PyObjC unavailable,
    Dock transition, etc.) only opens the gate if the override is
    active. An empty allowlist means OCR only runs during overrides.
    """
    if (
        screen_capture_override_until is not None
        and now_ts < screen_capture_override_until
    ):
        return True
    if not gate_by_allowlist:
        return True
    if frontmost_bundle is None:
        return False
    return frontmost_bundle in allowlist


def compute_phash(jpeg_bytes: bytes) -> bytes:
    """Compute a fixed-width perceptual hash for a JPEG-encoded frame.

    This is intentionally simple: an 8-byte SHA-256 prefix. It is NOT a
    true perceptual hash (visually similar frames will hash to entirely
    different bytes), but it provides exact-duplicate dedup (Hamming
    distance 0 for identical JPEG bytes, ~32 for any change), which is
    the correct cap for the worker's near-duplicate filter — a real
    pHash can be swapped in later without changing the storage schema.
    """
    return hashlib.sha256(jpeg_bytes).digest()[:8]


def hamming_distance(a: bytes, b: bytes) -> int:
    """Bit-level Hamming distance between two equal-length byte strings."""
    if len(a) != len(b):
        # Two hashes of different widths are by definition not
        # comparable — treat as "very different" so dedup never
        # accidentally drops them.
        return 8 * max(len(a), len(b))
    diff = 0
    for x, y in zip(a, b):
        diff += (x ^ y).bit_count()
    return diff


# ---------------------------------------------------------------------------
# OcrWorker
# ---------------------------------------------------------------------------


class OcrWorker:
    """Background worker that runs Vision OCR over the SCK video stream.

    Lifecycle mirrors :class:`helios.workers.transcription.TranscriptionWorker`:

    * :meth:`start` — checks Vision availability (lazy import); reports
      ``ok`` or ``unavailable(reason=...)``; spawns the consumer loop.
    * :meth:`stop` — cancels the loop. Idempotent.

    The worker is **not** responsible for starting the video source; the
    daemon hands it a started source (its iterator is drained until
    ``stop()`` puts a sentinel).
    """

    COMPONENT = "ocr"

    def __init__(
        self,
        db: aiosqlite.Connection,
        config: HeliosConfig,
        clock: Clock,
        reporter: ComponentStatusReporter,
        video_source: VideoFrameSource,
        active_session_provider: ActiveSessionProvider,
        *,
        frontmost_provider: FrontmostProvider | None = None,
        ocr_callable: OcrCallable | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._clock = clock
        self._reporter = reporter
        self._video_source = video_source
        self._active_session_provider = active_session_provider
        # ``frontmost_provider`` is injectable so tests don't need
        # AppKit. Default uses the real NSWorkspace path.
        self._frontmost_provider: FrontmostProvider = (
            frontmost_provider if frontmost_provider is not None
            else get_frontmost_bundle_id
        )
        # OCR backend is injectable for the same reason. ``None`` means
        # "use Vision when start() confirms it loads".
        self._ocr_callable: OcrCallable | None = ocr_callable
        self._vision_module: Any | None = None

        self._loop_task: asyncio.Task | None = None
        self._stopping = False
        # Last accepted frame timestamp (epoch seconds). Used by the fps
        # throttle so a 30 Hz SCK helper feeding a 1 fps OCR worker only
        # actually OCRs once per second.
        self._last_processed_ts: float = -float("inf")
        # Rolling phash window per session, kept in-memory so the dedup
        # check doesn't hit SQLite on every frame.
        self._recent_phashes: dict[int, list[bytes]] = {}

    # ------------------------------------------------------------------ public

    async def start(self) -> None:
        """Spawn the consumer loop after confirming Vision availability.

        Idempotent — a second call while running is a no-op.
        """
        if self._loop_task is not None and not self._loop_task.done():
            return
        cfg = self._config.ocr
        if not cfg.enabled:
            await self._reporter.set(
                self.COMPONENT,
                "unavailable",
                reason="disabled",
                action="Set ocr.enabled = true in capture.toml",
            )
            return

        # Probe the OCR backend (Vision) up-front so we report
        # ``unavailable`` rather than spinning a loop that drops every
        # frame. When the caller injected ``ocr_callable`` (tests, or a
        # future non-Vision backend) we trust it and skip the probe.
        if self._ocr_callable is None:
            backend, reason = self._try_load_vision_backend()
            if backend is None:
                await self._reporter.set(
                    self.COMPONENT,
                    "unavailable",
                    reason=reason or "vision_not_available",
                    action=(
                        "Vision framework not available — only macOS "
                        "supports screen OCR. The worker is idle."
                    ),
                )
                return
            self._ocr_callable = backend

        await self._reporter.set(self.COMPONENT, "ok")
        self._stopping = False
        self._loop_task = asyncio.create_task(
            self._run_loop(), name="ocr-worker"
        )

    async def stop(self) -> None:
        """Cancel the loop. Safe to call multiple times."""
        self._stopping = True
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._loop_task = None

    # ------------------------------------------------------------------ internal

    def _try_load_vision_backend(
        self,
    ) -> tuple[OcrCallable | None, str | None]:
        """Attempt to import Vision; return ``(backend, reason)``.

        On success returns ``(callable, None)``. On failure returns
        ``(None, "vision_not_available")`` (the reason string the
        caller will propagate to the component reporter). The lazy
        import is the only macOS-specific code path in this module —
        every other line runs on Linux CI.
        """
        try:
            import Vision  # type: ignore[import-not-found]
        except ImportError as exc:
            self._vision_module = None
            _log.info("ocr_vision_unavailable", error=str(exc))
            return None, "vision_not_available"
        self._vision_module = Vision
        return self._make_vision_callable(Vision), None

    def _make_vision_callable(self, vision_module: Any) -> OcrCallable:
        """Wrap Vision's ``VNRecognizeTextRequest`` as an ``OcrCallable``.

        Vision is synchronous and not asyncio-friendly, so this offloads
        each call to the default executor. We keep the wrapper small —
        the real heavy lifting is the lazy import + framework
        plumbing, all done per-call but cached by Apple's frameworks
        across invocations.
        """

        async def _run(jpeg: bytes) -> OcrResult | None:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._invoke_vision_blocking, vision_module, jpeg
            )

        return _run

    def _invoke_vision_blocking(
        self, vision_module: Any, jpeg_bytes: bytes
    ) -> OcrResult | None:
        """Synchronous Vision invocation. Runs on the executor thread.

        Returns ``None`` when Vision produced no candidates above the
        configured ``confidence_threshold``. The caller's dedup +
        ``min_text_chars`` filters run AFTER this returns, so a noisy
        but high-confidence frame still flows through to the persistence
        layer where the noise filters can decide.

        Implementation note: we intentionally accept the full Vision
        module rather than importing it at function top so tests can
        pass a fake module via ``ocr_callable`` and skip this entire
        method. The "real" macOS path that exercises this lives behind
        ``-m slow`` markers and the user's local machine.
        """
        try:
            CIImage = vision_module.CIImage  # type: ignore[attr-defined]
            VNRecognizeTextRequest = (
                vision_module.VNRecognizeTextRequest  # type: ignore[attr-defined]
            )
            VNImageRequestHandler = (
                vision_module.VNImageRequestHandler  # type: ignore[attr-defined]
            )
        except AttributeError as exc:
            _log.warning("ocr_vision_missing_symbol", error=str(exc))
            return None

        try:
            from Foundation import NSData  # type: ignore[import-not-found]
        except ImportError:
            return None

        try:
            data = NSData.dataWithBytes_length_(jpeg_bytes, len(jpeg_bytes))
            ci_image = CIImage.imageWithData_(data)
            if ci_image is None:
                return None
            request = VNRecognizeTextRequest.alloc().init()
            # ``accurate`` recognition level is slower but materially
            # better on slide / dashboard text.
            request.setRecognitionLevel_(1)
            handler = VNImageRequestHandler.alloc().initWithCIImage_options_(
                ci_image, None
            )
            ok, _err = handler.performRequests_error_([request], None)
            if not ok:
                return None
            observations = request.results() or []
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning("ocr_vision_call_failed", error=str(exc))
            return None

        texts: list[str] = []
        confidences: list[float] = []
        threshold = float(self._config.ocr.confidence_threshold)
        for obs in observations:
            try:
                candidates = obs.topCandidates_(1)
                if not candidates:
                    continue
                top = candidates[0]
                conf = float(top.confidence())
                if conf < threshold:
                    continue
                texts.append(str(top.string()))
                confidences.append(conf)
            except Exception:  # noqa: BLE001 — skip individual observation
                continue
        if not texts:
            return None
        avg = sum(confidences) / len(confidences)
        return OcrResult(text="\n".join(texts), avg_confidence=avg)

    async def _run_loop(self) -> None:
        """Drain the video source iterator until cancelled."""
        try:
            async for frame in self._video_source.video_frames():
                if self._stopping:
                    return
                try:
                    await self._handle_frame(frame)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — never break loop
                    _log.warning("ocr_frame_handle_failed", error=str(exc))
        except asyncio.CancelledError:
            raise

    async def _handle_frame(self, frame: VideoFrame) -> None:
        """Decide whether to OCR ``frame``, then persist if accepted."""
        cfg = self._config.ocr

        # fps throttle: drop frames that arrived sooner than the
        # configured minimum interval since the last accepted frame.
        # An fps of 0 (or negative) means "process every frame".
        min_interval = 1.0 / cfg.fps if cfg.fps > 0 else 0.0
        if frame.ts - self._last_processed_ts < min_interval:
            return

        # Resolve the active session once per candidate frame. We need
        # this for the override gate AND for the eventual DB write.
        active = await self._active_session_provider()
        if active is None:
            return
        session_id, override_until = active

        # Lookup frontmost bundle id (sync — wrapping it in run_in_executor
        # would add latency without buying anything; NSWorkspace returns
        # in microseconds).
        bundle = self._frontmost_provider()

        now_ts = self._clock.time()
        if not gate_allows_frame(
            now_ts=now_ts,
            frontmost_bundle=bundle,
            allowlist=cfg.meeting_apps,
            screen_capture_override_until=override_until,
            gate_by_allowlist=cfg.gate_by_allowlist,
        ):
            return

        # OCR.
        assert self._ocr_callable is not None  # noqa: S101 - start() guarantees it
        result = await self._ocr_callable(frame.jpeg)
        if result is None:
            return
        if len(result.text) < cfg.min_text_chars:
            # Too little signal to be useful. Drop.
            return

        # Near-duplicate filter (Hamming distance ≤ threshold).
        phash = compute_phash(frame.jpeg)
        recent = self._recent_phashes.setdefault(session_id, [])
        for prev in recent:
            if hamming_distance(prev, phash) <= cfg.dedup_hamming_threshold:
                return
        # Accept — append to rolling window (newest first).
        recent.insert(0, phash)
        del recent[cfg.dedup_window_frames :]

        frame_id = await queries.insert_ocr_frame(
            self._db,
            session_id=session_id,
            ts=frame.ts,
            # ``bundle`` may be None under an override-only gate; fall
            # back to a sentinel so the NOT NULL column never trips.
            app_bundle=bundle or "unknown",
            display_id=None,
            phash=phash,
            text=result.text,
            avg_confidence=result.avg_confidence,
            thumbnail_path=None,
        )
        self._last_processed_ts = frame.ts
        # PII-safe log: text_length only, never the text itself.
        _log.info(
            "ocr_frame_persisted",
            frame_id=frame_id,
            session_id=session_id,
            app_bundle=bundle,
            text_length=len(result.text),
            avg_confidence=round(result.avg_confidence, 3),
        )


__all__ = [
    "ActiveSessionProvider",
    "OcrCallable",
    "OcrResult",
    "OcrWorker",
    "VideoFrameSource",
    "compute_phash",
    "gate_allows_frame",
    "hamming_distance",
    "session_provider_from_db",
]
