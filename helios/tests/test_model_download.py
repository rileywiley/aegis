"""Tests for ``helios.menubar.model_download`` (Track 4D.6).

We never spawn a real subprocess. ``subprocess.Popen`` is replaced with
a fake that exposes a synchronous, line-by-line stdout pipe and lets
each test script the exit code + stdout content.

Callbacks fire from the reader thread, so each test thread-joins on the
ModelDownloader's worker before asserting (waiting on the captured
events). A small timeout on the joins prevents the whole suite from
hanging if a regression breaks the reader's exit path.
"""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from helios.menubar import model_download as md
from helios.menubar.model_download import ModelDownloader


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


class _FakeProc:
    """Subset of subprocess.Popen used by ModelDownloader.

    ``stdout_lines`` is the list of complete lines (without trailing \n)
    the reader thread will see. ``returncode_after_drain`` is the exit
    code reported once the pipe has been fully consumed.
    """

    def __init__(
        self,
        stdout_lines: List[str],
        returncode_after_drain: int = 0,
    ) -> None:
        # Each line gets its newline appended so the reader's `for line in stdout`
        # iteration produces one element per scripted line.
        self._stdout_text = "".join(line + "\n" for line in stdout_lines)
        self.stdout = io.StringIO(self._stdout_text)
        self.stderr = io.StringIO("")
        self._returncode_after_drain = returncode_after_drain
        self.returncode: Optional[int] = None
        self._terminated = False
        self._killed = False
        self._wait_event = threading.Event()
        # Track method calls for assertions.
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> Optional[int]:
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        # Set the returncode once the reader thread has drained stdout.
        # That mirrors real subprocess behaviour where the parent gets
        # EOF on the pipe shortly before the child exits.
        if self.returncode is None:
            self.returncode = self._returncode_after_drain
        self._wait_event.set()
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._terminated = True
        # A real terminated process exits non-zero; we set the rc here
        # so the reader's wait() sees it immediately.
        if self.returncode is None:
            self.returncode = -15  # SIGTERM

    def send_signal(self, _sig) -> None:
        self.kill_calls += 1
        if self.returncode is None:
            self.returncode = -9


class _Capture:
    """Collects callback invocations from the reader thread."""

    def __init__(self) -> None:
        self.progress: List[Tuple[float, str]] = []
        self.done_count = 0
        self.errors: List[str] = []
        self.terminal = threading.Event()

    def on_progress(self, fraction: float, filename: str) -> None:
        self.progress.append((fraction, filename))

    def on_done(self) -> None:
        self.done_count += 1
        self.terminal.set()

    def on_error(self, message: str) -> None:
        self.errors.append(message)
        self.terminal.set()


def _patch_popen(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> None:
    """Install ``proc`` as the result of ``subprocess.Popen(...)``.

    The fake is constructed up-front; we simply hand it back.
    """
    def _factory(*_args, **_kwargs) -> _FakeProc:
        return proc

    monkeypatch.setattr(md.subprocess, "Popen", _factory)


def _wait_for_terminal(cap: _Capture, timeout: float = 3.0) -> None:
    if not cap.terminal.wait(timeout=timeout):
        raise AssertionError("terminal callback never fired")


def _join_thread(dl: ModelDownloader, timeout: float = 3.0) -> None:
    if dl._thread is not None:
        dl._thread.join(timeout=timeout)
        if dl._thread.is_alive():
            raise AssertionError("reader thread still alive after timeout")


# ---------------------------------------------------------------------------
# 1. Happy path: info → 5 progress lines → done, exit 0
# ---------------------------------------------------------------------------


def test_happy_path_progress_then_done(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        '{"type":"info","repo_id":"Systran/faster-distil-whisper-large-v3"}',
        '{"type":"progress","filename":"model.bin","downloaded":0,"total":1000}',
        '{"type":"progress","filename":"model.bin","downloaded":250,"total":1000}',
        '{"type":"progress","filename":"model.bin","downloaded":500,"total":1000}',
        '{"type":"progress","filename":"model.bin","downloaded":750,"total":1000}',
        '{"type":"progress","filename":"model.bin","downloaded":1000,"total":1000}',
        '{"type":"done","path":"/tmp/snap/abc"}',
    ]
    proc = _FakeProc(lines, returncode_after_drain=0)
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    _wait_for_terminal(cap)
    _join_thread(dl)

    assert cap.errors == []
    assert cap.done_count == 1
    assert len(cap.progress) == 5
    fractions = [p[0] for p in cap.progress]
    assert fractions == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0])
    assert all(p[1] == "model.bin" for p in cap.progress)


# ---------------------------------------------------------------------------
# 2. Subprocess exits non-zero → on_error
# ---------------------------------------------------------------------------


def test_nonzero_exit_triggers_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # No done line, no error line; just a single progress and then exit 2.
    lines = [
        '{"type":"progress","filename":"model.bin","downloaded":50,"total":100}',
    ]
    proc = _FakeProc(lines, returncode_after_drain=2)
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    _wait_for_terminal(cap)
    _join_thread(dl)

    assert cap.done_count == 0
    assert len(cap.errors) == 1
    assert "subprocess_exited_nonzero" in cap.errors[0]
    assert "2" in cap.errors[0]


# ---------------------------------------------------------------------------
# 3. JSON parse error mid-stream → on_error + subprocess terminated
# ---------------------------------------------------------------------------


def test_parse_error_terminates_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed JSON (starts with ``{`` but doesn't parse) is fatal.

    A line that opens with ``{`` is claiming to be a structured payload;
    if it doesn't parse the subprocess is in an unrecoverable state.
    Non-``{`` lines (library warnings, stderr noise) are tolerated by
    a separate path — see ``test_non_json_lines_are_tolerated``.
    """
    lines = [
        '{"type":"progress","filename":"model.bin","downloaded":10,"total":100}',
        '{not really json',
        '{"type":"progress","filename":"model.bin","downloaded":20,"total":100}',  # ignored
    ]
    proc = _FakeProc(lines, returncode_after_drain=0)
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    _wait_for_terminal(cap)
    _join_thread(dl)

    assert cap.done_count == 0
    assert len(cap.errors) == 1
    assert cap.errors[0].startswith("parse_error")
    assert proc.terminate_calls >= 1
    # Only the first (valid) progress line should have been dispatched.
    assert len(cap.progress) == 1


def test_non_json_lines_are_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain-text lines from interleaved stderr (warnings, etc.) must not
    abort the run — they're skipped and JSON lines around them still
    dispatch normally. Regression: ``RequestsDependencyWarning`` from
    ``import requests`` was killing every onboarding model download.
    """
    lines = [
        "/path/requests/__init__.py:92: RequestsDependencyWarning: ...",
        "  warnings.warn(",
        '{"type":"progress","filename":"model.bin","downloaded":10,"total":100}',
        '{"type":"progress","filename":"model.bin","downloaded":100,"total":100}',
        '{"type":"done","path":"/tmp/model"}',
    ]
    proc = _FakeProc(lines, returncode_after_drain=0)
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    _wait_for_terminal(cap)
    _join_thread(dl)

    assert cap.errors == []
    assert cap.done_count == 1
    # Both progress payloads got through; the warning text in between
    # was silently skipped.
    assert len(cap.progress) == 2


# ---------------------------------------------------------------------------
# 4. Error line → on_error("download_failed: ...")
# ---------------------------------------------------------------------------


def test_error_line_dispatches_with_code_and_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [
        '{"type":"info","repo_id":"Systran/faster-distil-whisper-large-v3"}',
        '{"type":"error","code":"download_failed","detail":"connection reset"}',
    ]
    # Real download_whisper exits 2 after emitting download_failed.
    proc = _FakeProc(lines, returncode_after_drain=2)
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    _wait_for_terminal(cap)
    _join_thread(dl)

    assert cap.done_count == 0
    # Exactly one error fired — the structured one — even though the
    # subprocess also exited non-zero.
    assert len(cap.errors) == 1
    assert cap.errors[0] == "download_failed: connection reset"


def test_error_line_without_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        '{"type":"error","code":"insufficient_disk_space","needed_bytes":3,"free_bytes":1}',
    ]
    proc = _FakeProc(lines, returncode_after_drain=1)
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    _wait_for_terminal(cap)
    _join_thread(dl)

    assert cap.errors == ["insufficient_disk_space"]


# ---------------------------------------------------------------------------
# 5. Warning line → no error, no done; logged only
# ---------------------------------------------------------------------------


def test_warning_line_does_not_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        '{"type":"warning","code":"verification_skipped_no_whisperx"}',
        '{"type":"progress","filename":"model.bin","downloaded":1,"total":2}',
        '{"type":"done","path":"/tmp/x"}',
    ]
    proc = _FakeProc(lines, returncode_after_drain=0)
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    _wait_for_terminal(cap)
    _join_thread(dl)

    # Warning didn't trip on_error; download still finished cleanly.
    assert cap.errors == []
    assert cap.done_count == 1
    assert cap.progress == [(0.5, "model.bin")]


# ---------------------------------------------------------------------------
# 6. is_cached() False when cache dir doesn't exist
# ---------------------------------------------------------------------------


def test_is_cached_false_when_no_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Redirect HF cache to an empty tmpdir.
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    dl = ModelDownloader(
        on_progress=lambda *_: None,
        on_done=lambda: None,
        on_error=lambda *_: None,
    )
    assert dl.is_cached() is False


def test_is_cached_false_when_snapshots_dir_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cache root exists but the model's snapshots/ subdir doesn't."""
    hub = tmp_path / "hub"
    (hub / "models--Systran--faster-distil-whisper-large-v3").mkdir(parents=True)
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))

    dl = ModelDownloader(lambda *_: None, lambda: None, lambda *_: None)
    assert dl.is_cached() is False


def test_is_cached_false_when_snapshots_dir_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hub = tmp_path / "hub"
    (hub / "models--Systran--faster-distil-whisper-large-v3" / "snapshots").mkdir(
        parents=True
    )
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))

    dl = ModelDownloader(lambda *_: None, lambda: None, lambda *_: None)
    assert dl.is_cached() is False


# ---------------------------------------------------------------------------
# 7. is_cached() True when snapshot dir exists
# ---------------------------------------------------------------------------


def test_is_cached_true_when_snapshot_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hub = tmp_path / "hub"
    (
        hub
        / "models--Systran--faster-distil-whisper-large-v3"
        / "snapshots"
        / "abcdef1234"
    ).mkdir(parents=True)
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))

    dl = ModelDownloader(lambda *_: None, lambda: None, lambda *_: None)
    assert dl.is_cached() is True


def test_is_cached_respects_hf_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When HF_HUB_CACHE unset but HF_HOME set, root is $HF_HOME/hub."""
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    (
        tmp_path
        / "hub"
        / "models--Systran--faster-distil-whisper-large-v3"
        / "snapshots"
        / "snap1"
    ).mkdir(parents=True)

    dl = ModelDownloader(lambda *_: None, lambda: None, lambda *_: None)
    assert dl.is_cached() is True


def test_is_cached_uses_model_specific_dirname(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-default model name maps to its own cache dir."""
    hub = tmp_path / "hub"
    (
        hub
        / "models--Systran--faster-whisper-small.en"
        / "snapshots"
        / "snap1"
    ).mkdir(parents=True)
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))

    dl = ModelDownloader(
        lambda *_: None,
        lambda: None,
        lambda *_: None,
        model="small.en",
    )
    assert dl.is_cached() is True
    # And the default model name does NOT match this cache layout.
    dl_default = ModelDownloader(lambda *_: None, lambda: None, lambda *_: None)
    assert dl_default.is_cached() is False


# ---------------------------------------------------------------------------
# 8. cancel() before start → no-op (no crash)
# ---------------------------------------------------------------------------


def test_cancel_before_start_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    # No Popen patched — cancel() must not try to touch a process.
    dl.cancel()
    # No exception, no callbacks fired.
    assert cap.progress == []
    assert cap.done_count == 0
    assert cap.errors == []

    # If the user later calls start() after a cancel, we surface a
    # single on_error("cancelled") rather than spawning a doomed run.
    # (Documented behaviour — keeps the UI from hanging.)
    cap2 = _Capture()
    dl2 = ModelDownloader(cap2.on_progress, cap2.on_done, cap2.on_error)
    dl2.cancel()
    dl2.start()
    assert cap2.errors == ["cancelled"]
    assert cap2.done_count == 0


# ---------------------------------------------------------------------------
# 9. cancel() during running → terminate() called
# ---------------------------------------------------------------------------


def test_cancel_during_running_terminates(monkeypatch: pytest.MonkeyPatch) -> None:
    # A long-running stdout: blocks the reader thread until we cancel.
    class _BlockingProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__([], returncode_after_drain=-15)
            # Replace stdout with a blocking iterator.
            self._unblock = threading.Event()
            self.stdout = self  # we implement __iter__ ourselves

        def __iter__(self):
            return self

        def __next__(self):
            # Block until the test calls cancel(); on terminate we set
            # the event from inside terminate() to release the iterator.
            self._unblock.wait(timeout=3.0)
            raise StopIteration

        def terminate(self) -> None:
            self.terminate_calls += 1
            self._terminated = True
            if self.returncode is None:
                self.returncode = -15
            self._unblock.set()

    proc = _BlockingProc()
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    # Give the reader thread a moment to enter its read loop.
    threading.Event().wait(0.05)
    dl.cancel()

    _wait_for_terminal(cap)
    _join_thread(dl)

    assert proc.terminate_calls >= 1
    assert cap.done_count == 0
    assert cap.errors == ["cancelled"]


# ---------------------------------------------------------------------------
# 10. fraction = 0.0 when total=0
# ---------------------------------------------------------------------------


def test_fraction_zero_when_total_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        '{"type":"progress","filename":"weights","downloaded":0,"total":0}',
        '{"type":"progress","filename":"weights","downloaded":1024,"total":0}',
        '{"type":"progress","filename":"weights","downloaded":1,"total":2}',
        '{"type":"done","path":"/tmp/x"}',
    ]
    proc = _FakeProc(lines, returncode_after_drain=0)
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    _wait_for_terminal(cap)
    _join_thread(dl)

    assert cap.errors == []
    assert cap.done_count == 1
    assert len(cap.progress) == 3
    # First two lines: total=0 → fraction is exactly 0.0
    assert cap.progress[0] == (0.0, "weights")
    assert cap.progress[1] == (0.0, "weights")
    assert cap.progress[2] == (0.5, "weights")


# ---------------------------------------------------------------------------
# 11. Multiple progress lines in same filename — last one wins; per-line dispatch
# ---------------------------------------------------------------------------


def test_progress_dispatches_per_line(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        '{"type":"progress","filename":"model.bin","downloaded":1,"total":4}',
        '{"type":"progress","filename":"model.bin","downloaded":2,"total":4}',
        '{"type":"progress","filename":"model.bin","downloaded":3,"total":4}',
        '{"type":"progress","filename":"model.bin","downloaded":4,"total":4}',
        '{"type":"done","path":"/p"}',
    ]
    proc = _FakeProc(lines, returncode_after_drain=0)
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    _wait_for_terminal(cap)
    _join_thread(dl)

    assert len(cap.progress) == 4
    # Per-line dispatch, monotonic, last is 1.0 ("last one wins" check):
    assert cap.progress[-1] == (1.0, "model.bin")
    assert cap.progress[0][0] < cap.progress[-1][0]


# ---------------------------------------------------------------------------
# 12. Progress for different filenames over time
# ---------------------------------------------------------------------------


def test_progress_handles_filename_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        '{"type":"progress","filename":"config.json","downloaded":50,"total":100}',
        '{"type":"progress","filename":"config.json","downloaded":100,"total":100}',
        '{"type":"progress","filename":"model.bin","downloaded":0,"total":1024}',
        '{"type":"progress","filename":"model.bin","downloaded":512,"total":1024}',
        '{"type":"progress","filename":"model.bin","downloaded":1024,"total":1024}',
        '{"type":"done","path":"/p"}',
    ]
    proc = _FakeProc(lines, returncode_after_drain=0)
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    _wait_for_terminal(cap)
    _join_thread(dl)

    filenames = [p[1] for p in cap.progress]
    assert filenames == [
        "config.json",
        "config.json",
        "model.bin",
        "model.bin",
        "model.bin",
    ]
    fractions = [p[0] for p in cap.progress]
    assert fractions == pytest.approx([0.5, 1.0, 0.0, 0.5, 1.0])


# ---------------------------------------------------------------------------
# Extra: double start() is a no-op
# ---------------------------------------------------------------------------


def test_double_start_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = ['{"type":"done","path":"/p"}']
    proc = _FakeProc(lines, returncode_after_drain=0)
    popen_mock = MagicMock(return_value=proc)
    monkeypatch.setattr(md.subprocess, "Popen", popen_mock)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    dl.start()  # second call: ignored
    _wait_for_terminal(cap)
    _join_thread(dl)

    assert popen_mock.call_count == 1


# ---------------------------------------------------------------------------
# Extra: spawn failure (Popen raises OSError) routes through on_error
# ---------------------------------------------------------------------------


def test_spawn_failure_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args, **_kwargs):
        raise OSError("no such executable")

    monkeypatch.setattr(md.subprocess, "Popen", _boom)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    # No reader thread to join — start() called on_error synchronously.
    assert cap.done_count == 0
    assert len(cap.errors) == 1
    assert cap.errors[0].startswith("spawn_failed")


# ---------------------------------------------------------------------------
# Extra: callback exception in on_progress doesn't crash the reader
# ---------------------------------------------------------------------------


def test_progress_callback_exception_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [
        '{"type":"progress","filename":"m","downloaded":1,"total":2}',
        '{"type":"progress","filename":"m","downloaded":2,"total":2}',
        '{"type":"done","path":"/p"}',
    ]
    proc = _FakeProc(lines, returncode_after_drain=0)
    _patch_popen(monkeypatch, proc)

    call_count = {"n": 0}

    def crashy_progress(_f: float, _n: str) -> None:
        call_count["n"] += 1
        raise RuntimeError("ui exploded")

    cap_done = threading.Event()
    cap_errors: List[str] = []

    def on_done() -> None:
        cap_done.set()

    def on_error(msg: str) -> None:
        cap_errors.append(msg)
        cap_done.set()

    dl = ModelDownloader(crashy_progress, on_done, on_error)
    dl.start()
    assert cap_done.wait(timeout=3.0)
    _join_thread(dl)

    # Both progress lines were dispatched even though each crashed; done fired.
    assert call_count["n"] == 2
    assert cap_errors == []


# ---------------------------------------------------------------------------
# Extra: clean exit 0 but no done line is treated as error
# ---------------------------------------------------------------------------


def test_clean_exit_without_done_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        '{"type":"progress","filename":"m","downloaded":1,"total":2}',
    ]
    proc = _FakeProc(lines, returncode_after_drain=0)
    _patch_popen(monkeypatch, proc)

    cap = _Capture()
    dl = ModelDownloader(cap.on_progress, cap.on_done, cap.on_error)
    dl.start()
    _wait_for_terminal(cap)
    _join_thread(dl)

    assert cap.done_count == 0
    assert cap.errors == ["subprocess_exited_without_done"]
