"""Model download driver for the menu-bar onboarding flow (Track 4D.6).

Spawns the ``helios.scripts.download_whisper`` subprocess, parses its
JSON-line stdout in a background thread, and dispatches three callbacks
back to the caller:

  * ``on_progress(fraction: float, filename: str)`` — fired once per
    ``{"type": "progress", ...}`` line.
  * ``on_done()`` — fired exactly once when a ``{"type": "done"}`` line
    has been seen AND the subprocess exited 0.
  * ``on_error(message: str)`` — fired on any of:
      - a ``{"type": "error", "code": ..., "detail"?: ...}`` line,
      - a non-zero subprocess exit (when ``done`` was not seen),
      - a stdout line that fails to parse as JSON.

The caller (``OnboardingWindow``) is sync — it lives on the rumps /
AppKit runloop, not asyncio. So this driver is sync too. Subprocess
management runs in a single background thread (``threading.Thread``);
all three callbacks are dispatched FROM THAT THREAD. Callers that need
to touch UIKit/AppKit state from a callback are responsible for hopping
to the main thread themselves (e.g. via ``rumps.Timer`` or
``NSOperationQueue.mainQueue()``); we do not wrap that here because
it would couple this module to PyObjC and break tests on non-macOS CI.

PII safety: log lines carry only model name, fraction (rounded), and
filename (which is the HF asset filename like ``model.bin``, never user
content). Stdout lines from the subprocess are NOT logged verbatim.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from helios.log import get_logger

_log = get_logger("menubar.model_download")


# Module of the download script. Using ``-m`` (rather than a path) keeps
# the subprocess's ``sys.path`` aligned with the parent's environment.
_DOWNLOAD_MODULE = "helios.scripts.download_whisper"

# Cache layout used by huggingface_hub for the default distil-large-v3 model.
# The repo id is "Systran/faster-distil-whisper-large-v3" → cache dir is
# "models--Systran--faster-distil-whisper-large-v3" under the HF hub root.
_DEFAULT_MODEL = "distil-large-v3"
_DEFAULT_REPO_DIRNAME = "models--Systran--faster-distil-whisper-large-v3"
_MODEL_REPO_DIRNAMES = {
    "tiny": "models--Systran--faster-whisper-tiny",
    "tiny.en": "models--Systran--faster-whisper-tiny.en",
    "base": "models--Systran--faster-whisper-base",
    "base.en": "models--Systran--faster-whisper-base.en",
    "small": "models--Systran--faster-whisper-small",
    "small.en": "models--Systran--faster-whisper-small.en",
    "medium": "models--Systran--faster-whisper-medium",
    "medium.en": "models--Systran--faster-whisper-medium.en",
    "large-v1": "models--Systran--faster-whisper-large-v1",
    "large-v2": "models--Systran--faster-whisper-large-v2",
    "large-v3": "models--Systran--faster-whisper-large-v3",
    "distil-small.en": "models--Systran--faster-distil-whisper-small.en",
    "distil-medium.en": "models--Systran--faster-distil-whisper-medium.en",
    "distil-large-v2": "models--Systran--faster-distil-whisper-large-v2",
    "distil-large-v3": "models--Systran--faster-distil-whisper-large-v3",
}


def _hf_hub_root() -> Path:
    """Return the HF hub cache root, honouring HF_HOME / HF_HUB_CACHE.

    Mirrors huggingface_hub's resolution order:
      1. ``HF_HUB_CACHE`` if set → that dir directly.
      2. ``HF_HOME`` if set → ``$HF_HOME/hub``.
      3. Else ``~/.cache/huggingface/hub``.
    """
    env_hub = os.environ.get("HF_HUB_CACHE")
    if env_hub:
        return Path(env_hub)
    env_home = os.environ.get("HF_HOME")
    if env_home:
        return Path(env_home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _repo_dirname_for(model: str) -> str:
    """Map a short model name → HF cache directory name."""
    if model in _MODEL_REPO_DIRNAMES:
        return _MODEL_REPO_DIRNAMES[model]
    if "/" in model:
        # explicit "org/repo" → "models--org--repo"
        return "models--" + model.replace("/", "--")
    # fallback: same convention as download_whisper._resolve_repo_id
    return f"models--Systran--faster-whisper-{model}"


class ModelDownloader:
    """Drives the ``helios.scripts.download_whisper`` subprocess.

    Parses JSON progress lines from stdout, dispatches to callbacks.

    Usage::

        dl = ModelDownloader(
            on_progress=lambda f, n: print(f"{f:.0%} {n}"),
            on_done=lambda: print("done"),
            on_error=lambda m: print(f"err: {m}"),
        )
        if not dl.is_cached():
            dl.start()
        # later, if user clicks Cancel:
        dl.cancel()

    Callbacks fire from a background thread. ``start()`` is non-blocking.
    ``cancel()`` is idempotent and safe to call before ``start()`` or
    after the process has already exited.
    """

    def __init__(
        self,
        on_progress: Callable[[float, str], None],
        on_done: Callable[[], None],
        on_error: Callable[[str], None],
        model: str = _DEFAULT_MODEL,
    ) -> None:
        self._on_progress = on_progress
        self._on_done = on_done
        self._on_error = on_error
        self._model = model

        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        # Reentrant: start() holds the lock and may call _fire_error()
        # synchronously (e.g. cancel-before-start), which also acquires
        # the lock. RLock keeps that path single-threaded without deadlock.
        self._lock = threading.RLock()
        self._cancelled = False
        # Set once a {"type": "done"} line has been observed.
        self._saw_done = False
        # Set once exactly one terminal callback (on_done OR on_error) has
        # fired, so we never double-dispatch even if cancel races stdout.
        self._terminal_fired = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the download subprocess and start the reader thread.

        Calling start() twice is a no-op (the second call is ignored). If
        a previous run has finished and you need to retry, instantiate a
        new ``ModelDownloader``.
        """
        with self._lock:
            if self._thread is not None:
                _log.debug("model_download.start.ignored", reason="already_started")
                return
            if self._cancelled:
                # A cancel() before start() — honour it; emit a single
                # error so the caller's UI doesn't hang.
                _log.info("model_download.start.cancelled_before_start")
                self._fire_error("cancelled")
                return

            cmd = [
                sys.executable,
                "-m",
                _DOWNLOAD_MODULE,
                "--model",
                self._model,
            ]
            # When this module is loaded inside a py2app bundle, the
            # parent process has ``helios.*`` on ``sys.path`` thanks to
            # py2app's bootstrap. A subprocess started via
            # ``sys.executable -m helios.scripts.…`` skips that
            # bootstrap and gets ModuleNotFoundError("No module named
            # 'helios.scripts'"). Forward our live ``sys.path`` to the
            # child via ``PYTHONPATH`` so the bundled site-packages
            # remain importable (idempotent for non-bundle / dev runs —
            # the dev venv's site-packages are already on sys.path).
            env = os.environ.copy()
            existing_pythonpath = env.get("PYTHONPATH", "")
            inherited_paths = os.pathsep.join(p for p in sys.path if p)
            env["PYTHONPATH"] = (
                f"{inherited_paths}{os.pathsep}{existing_pythonpath}"
                if existing_pythonpath
                else inherited_paths
            )
            try:
                # Merge stderr into stdout so the single read loop drains
                # both — using subprocess.PIPE on stderr without a reader
                # can deadlock the child once the OS pipe buffer (~64 KiB)
                # fills. Diagnostic output stays available via the merged
                # stream.
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    text=True,
                    env=env,
                )
            except OSError as exc:
                # No PII — just the model name + a short error class.
                _log.error(
                    "model_download.spawn_failed",
                    model=self._model,
                    error=type(exc).__name__,
                )
                self._fire_error(f"spawn_failed: {exc}")
                return

            self._thread = threading.Thread(
                target=self._read_loop,
                name="ModelDownloader",
                daemon=True,
            )
            self._thread.start()
            _log.info("model_download.started", model=self._model)

    def cancel(self) -> None:
        """Request cancellation. Idempotent.

        If the subprocess is running, sends SIGTERM and lets the reader
        thread observe the termination + drain remaining stdout. If
        ``start()`` was never called, just sets a flag so a future
        ``start()`` is a clean no-op (with on_error("cancelled")).
        """
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is None:
            _log.debug("model_download.cancel.no_proc")
            return
        if proc.poll() is not None:
            _log.debug("model_download.cancel.already_exited")
            return
        try:
            # SIGTERM first — gives huggingface_hub a chance to release its
            # in-flight connection cleanly. The reader thread will detect
            # exit and fire on_error("cancelled") if no done line arrived.
            proc.terminate()
            _log.info("model_download.cancelled", model=self._model)
        except (OSError, ProcessLookupError) as exc:
            _log.debug(
                "model_download.cancel.terminate_failed",
                error=type(exc).__name__,
            )

    def is_cached(self) -> bool:
        """Return True if the model snapshot exists in the HF hub cache.

        Conservative: returns False if uncertain (so the user sees a fresh
        download rather than a silent skip onto a corrupt cache). The
        underlying download_whisper script will still re-verify under
        WhisperX even when the files are already present.
        """
        repo_dir = _hf_hub_root() / _repo_dirname_for(self._model)
        snapshots = repo_dir / "snapshots"
        if not snapshots.is_dir():
            return False
        try:
            for entry in snapshots.iterdir():
                if entry.is_dir():
                    # At least one snapshot directory present.
                    return True
        except OSError:
            return False
        return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        """Read stdout line by line, dispatch callbacks. Runs in a thread."""
        proc = self._proc
        assert proc is not None and proc.stdout is not None  # noqa: S101

        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                # Stderr is merged into stdout (so a single reader drains
                # both, see start()). The subprocess inherits ``warnings``
                # from cpython and from the libraries it imports —
                # ``requests.RequestsDependencyWarning`` and friends print
                # plain text to stderr at import time, which arrives here
                # interleaved with the structured JSON ``_emit`` payloads.
                # Treat any line whose first non-whitespace char isn't ``{``
                # as a passthrough log line and skip it rather than killing
                # the subprocess as "flying blind". The JSON contract is
                # narrow enough that this rule has no false positives.
                if not line.startswith("{"):
                    _log.debug(
                        "model_download.subprocess_log",
                        line=line[:200],
                    )
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    _log.warning(
                        "model_download.parse_error",
                        model=self._model,
                        error=type(exc).__name__,
                        line=line[:200],
                    )
                    # Kill the subprocess — we're flying blind. The reader
                    # loop will exit naturally once the pipe closes.
                    self._terminate_proc()
                    self._fire_error(f"parse_error: {exc}")
                    # Drain remaining stdout so the OS-level pipe closes
                    # promptly and wait() doesn't hang.
                    self._drain_stdout(proc)
                    break

                ptype = payload.get("type")
                if ptype == "progress":
                    self._handle_progress(payload)
                elif ptype == "done":
                    self._saw_done = True
                    # Don't fire on_done yet — wait for clean exit so we
                    # don't claim success for a subprocess that's about
                    # to crash on the verification step.
                    _log.debug(
                        "model_download.done_line",
                        path=payload.get("path", ""),
                    )
                elif ptype == "error":
                    code = payload.get("code", "unknown_error")
                    detail = payload.get("detail")
                    msg = f"{code}: {detail}" if detail else code
                    self._fire_error(msg)
                    # Process should exit imminently. Don't terminate
                    # here — let it clean up. We've already fired the
                    # terminal callback so subsequent done lines are no-ops.
                elif ptype == "warning":
                    _log.info(
                        "model_download.warning",
                        model=self._model,
                        code=payload.get("code", "unknown"),
                    )
                elif ptype == "info":
                    _log.debug(
                        "model_download.info",
                        repo_id=payload.get("repo_id", ""),
                    )
                else:
                    # Unknown type — log but don't fail.
                    _log.debug(
                        "model_download.unknown_line_type",
                        ptype=str(ptype),
                    )
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "model_download.reader_crash",
                model=self._model,
                error=type(exc).__name__,
            )
            self._fire_error(f"reader_crash: {exc}")
            self._terminate_proc()

        # Wait for the subprocess to actually exit so returncode is set.
        try:
            proc.wait()
        except Exception:  # noqa: BLE001
            pass

        rc = proc.returncode
        _log.info(
            "model_download.exited",
            model=self._model,
            returncode=rc if rc is not None else -1,
            saw_done=self._saw_done,
        )

        # Final dispatch: did we end clean?
        if self._terminal_fired:
            return  # error or done already fired
        if rc == 0 and self._saw_done:
            self._fire_done()
        elif self._cancelled:
            self._fire_error("cancelled")
        elif rc != 0:
            self._fire_error(f"subprocess_exited_nonzero: {rc}")
        else:
            # Exited 0 but no done line — treat as error (download
            # script should always emit done on success).
            self._fire_error("subprocess_exited_without_done")

    def _handle_progress(self, payload: dict) -> None:
        """Compute fraction and dispatch on_progress."""
        downloaded = payload.get("downloaded", 0)
        total = payload.get("total", 0)
        filename = payload.get("filename", "")
        if not isinstance(downloaded, (int, float)):
            downloaded = 0
        if not isinstance(total, (int, float)):
            total = 0
        if not isinstance(filename, str):
            filename = str(filename)

        if total > 0:
            fraction = float(downloaded) / float(total)
            # Clamp — total can briefly under-report on multi-file repos.
            if fraction > 1.0:
                fraction = 1.0
            elif fraction < 0.0:
                fraction = 0.0
        else:
            fraction = 0.0

        try:
            self._on_progress(fraction, filename)
        except Exception as exc:  # noqa: BLE001
            # A crashing UI callback must not bring down the reader thread.
            _log.error(
                "model_download.progress_callback_crash",
                error=type(exc).__name__,
            )

    def _fire_done(self) -> None:
        with self._lock:
            if self._terminal_fired:
                return
            self._terminal_fired = True
        try:
            self._on_done()
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "model_download.done_callback_crash",
                error=type(exc).__name__,
            )

    def _fire_error(self, message: str) -> None:
        with self._lock:
            if self._terminal_fired:
                return
            self._terminal_fired = True
        # message is our own composed string, never raw subprocess output.
        try:
            self._on_error(message)
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "model_download.error_callback_crash",
                error=type(exc).__name__,
            )

    def _terminate_proc(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            pass
        # If terminate doesn't work within a short window, escalate to
        # SIGKILL. We don't wait long here — the reader thread is already
        # past its main loop when this is called.
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                proc.send_signal(signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        except Exception:  # noqa: BLE001
            pass

    def _drain_stdout(self, proc: subprocess.Popen) -> None:
        """Read and discard remaining stdout so the pipe closes."""
        if proc.stdout is None:
            return
        try:
            for _ in proc.stdout:
                pass
        except Exception:  # noqa: BLE001
            pass
