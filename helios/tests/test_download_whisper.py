"""Tests for helios.scripts.download_whisper (Wave 1A / Phase 3)."""

from __future__ import annotations

import builtins
import io
import json
import sys

import pytest

from helios.scripts import download_whisper as dw


# ---------------------------------------------------------------------------
# JSONProgressTqdm.display
# ---------------------------------------------------------------------------


def test_jsonprogress_tqdm_emits_json_lines(capsys):
    """Driving display() emits one line of valid progress JSON to stdout."""
    # file=StringIO routes tqdm's own terminal output to a discard buffer
    # while keeping desc/n/total populated. (disable=True would strip those
    # attributes — we want to test display() not the rendering pipeline.)
    bar = dw.JSONProgressTqdm(
        total=1000,
        desc="model.bin",
        file=io.StringIO(),
        mininterval=0,
    )
    bar.n = 250
    bar.display()
    bar.n = 1000
    bar.display()
    bar.close()

    out = capsys.readouterr().out
    lines = [ln for ln in out.strip().split("\n") if ln]
    parsed = [json.loads(ln) for ln in lines]
    assert len(parsed) >= 2
    for line in parsed:
        assert line["type"] == "progress"
        assert line["filename"] == "model.bin"
        assert isinstance(line["downloaded"], int)
        assert isinstance(line["total"], int)
    # Last emitted line should reflect downloaded == total.
    assert parsed[-1]["downloaded"] == 1000
    assert parsed[-1]["total"] == 1000


def test_jsonprogress_tqdm_unknown_total(capsys):
    """When total is None, emit total=0 (not None / not a crash)."""
    bar = dw.JSONProgressTqdm(
        total=None,
        desc="weights",
        file=io.StringIO(),
        mininterval=0,
    )
    bar.n = 42
    bar.display()
    bar.close()

    out = capsys.readouterr().out
    lines = [json.loads(ln) for ln in out.strip().split("\n") if ln]
    assert any(line["downloaded"] == 42 and line["total"] == 0 for line in lines)


# ---------------------------------------------------------------------------
# main() — exit codes + JSON contract
# ---------------------------------------------------------------------------


def _set_argv(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["download_whisper", *args])


def _last_json_line(capsys) -> dict:
    out = capsys.readouterr().out
    lines = [ln for ln in out.strip().split("\n") if ln]
    assert lines, "expected at least one stdout line"
    return json.loads(lines[-1])


def test_main_insufficient_disk_space_exits_1(monkeypatch, capsys):
    class FakeUsage:
        free = 100  # 100 bytes — way below the 3GB default threshold

    monkeypatch.setattr(dw.shutil, "disk_usage", lambda _path: FakeUsage())
    _set_argv(monkeypatch, "--model", "small.en")

    with pytest.raises(SystemExit) as exc:
        dw.main()
    assert exc.value.code == 1
    last = _last_json_line(capsys)
    assert last["type"] == "error"
    assert last["code"] == "insufficient_disk_space"
    assert last["free_bytes"] == 100


def test_main_download_failed_exits_2(monkeypatch, capsys):
    """A snapshot_download exception triggers exit 2 + a download_failed line."""

    class FakeUsage:
        free = 999 * 1024**3  # plenty of disk

    monkeypatch.setattr(dw.shutil, "disk_usage", lambda _path: FakeUsage())

    def boom(*_args, **_kwargs):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(dw.huggingface_hub, "snapshot_download", boom)
    _set_argv(monkeypatch, "--model", "distil-large-v3")

    with pytest.raises(SystemExit) as exc:
        dw.main()
    assert exc.value.code == 2
    last = _last_json_line(capsys)
    assert last["type"] == "error"
    assert last["code"] == "download_failed"
    assert "network exploded" in last["detail"]


def test_main_skip_verify_done_line(monkeypatch, capsys, tmp_path):
    """With --skip-verify, a successful download yields a {type:done} line."""

    class FakeUsage:
        free = 999 * 1024**3

    monkeypatch.setattr(dw.shutil, "disk_usage", lambda _path: FakeUsage())
    fake_path = tmp_path / "snapshots" / "fake-model"
    monkeypatch.setattr(
        dw.huggingface_hub,
        "snapshot_download",
        lambda **_kwargs: str(fake_path),
    )
    _set_argv(monkeypatch, "--model", "small.en", "--skip-verify")

    # main() returns normally on success (no SystemExit raised here).
    dw.main()
    last = _last_json_line(capsys)
    assert last == {"type": "done", "path": str(fake_path)}


# ---------------------------------------------------------------------------
# verify_with_synthetic_audio
# ---------------------------------------------------------------------------


def test_verify_skipped_when_whisperx_missing(monkeypatch, capsys):
    """If whisperx isn't importable, emit a warning JSON line and return."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "whisperx" or name.startswith("whisperx."):
            raise ImportError("no module named whisperx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Should NOT raise SystemExit.
    dw.verify_with_synthetic_audio("/tmp/fake")

    out = capsys.readouterr().out
    lines = [json.loads(ln) for ln in out.strip().split("\n") if ln]
    assert any(
        line.get("type") == "warning"
        and line.get("code") == "verification_skipped_no_whisperx"
        for line in lines
    )


def test_verify_failure_exits_3(monkeypatch, capsys):
    """If whisperx loads but model load raises, exit 3 + verification_failed line."""

    class _FakeWhisperX:
        @staticmethod
        def load_model(*_args, **_kwargs):
            raise RuntimeError("model corrupt")

    # Inject the fake into sys.modules so the lazy ``import whisperx`` finds it.
    monkeypatch.setitem(sys.modules, "whisperx", _FakeWhisperX)

    with pytest.raises(SystemExit) as exc:
        dw.verify_with_synthetic_audio("/tmp/fake")
    assert exc.value.code == 3
    last = _last_json_line(capsys)
    assert last["type"] == "error"
    assert last["code"] == "verification_failed"
    assert "model corrupt" in last["detail"]


def test_verify_success_no_emit(monkeypatch, capsys):
    """Successful verify should not exit and should not emit an error line."""

    class _FakeModel:
        def transcribe(self, _audio, language="en"):
            return {"segments": []}

    class _FakeWhisperX:
        @staticmethod
        def load_model(*_args, **_kwargs):
            return _FakeModel()

    monkeypatch.setitem(sys.modules, "whisperx", _FakeWhisperX)

    dw.verify_with_synthetic_audio("/tmp/fake", language="en")

    out = capsys.readouterr().out
    # Should be no error/warning lines — just silent success.
    for ln in out.strip().split("\n"):
        if not ln:
            continue
        parsed = json.loads(ln)
        assert parsed.get("type") not in ("error", "warning")
