"""Tests for OCR gating logic — Track 5A.

These are pure-function tests for ``gate_allows_frame``,
``compute_phash``, ``hamming_distance``, and the lazy
``get_frontmost_bundle_id`` helper. No worker / DB needed — the
worker-level tests live in :mod:`tests.test_ocr_worker`.

Mocking strategy for the AppKit branch mirrors
``tests/test_menubar_notifications.py``: we inject a fake ``AppKit``
module into ``sys.modules`` so the lazy import inside
:func:`helios.workers.frontmost.get_frontmost_bundle_id` resolves to our
stub. Linux CI is fine because the only mac-specific code in the
module is behind that lazy import.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from helios.workers import frontmost
from helios.workers.ocr import (
    compute_phash,
    gate_allows_frame,
    hamming_distance,
)


# ---------------------------------------------------------------------------
# gate_allows_frame
# ---------------------------------------------------------------------------


_ALLOW = ["com.microsoft.teams2", "us.zoom.xos"]


def test_allows_when_frontmost_in_allowlist():
    assert gate_allows_frame(
        now_ts=100.0,
        frontmost_bundle="com.microsoft.teams2",
        allowlist=_ALLOW,
        screen_capture_override_until=None,
    )


def test_blocks_when_frontmost_not_in_allowlist():
    assert not gate_allows_frame(
        now_ts=100.0,
        frontmost_bundle="com.apple.mail",
        allowlist=_ALLOW,
        screen_capture_override_until=None,
    )


def test_blocks_when_frontmost_none_and_no_override():
    """No PyObjC / no frontmost app ⇒ fail-closed."""
    assert not gate_allows_frame(
        now_ts=100.0,
        frontmost_bundle=None,
        allowlist=_ALLOW,
        screen_capture_override_until=None,
    )


def test_override_in_future_opens_gate_regardless_of_app():
    """User-initiated override beats allowlist for its duration."""
    assert gate_allows_frame(
        now_ts=100.0,
        frontmost_bundle="com.apple.mail",
        allowlist=_ALLOW,
        screen_capture_override_until=200.0,
    )


def test_override_in_past_does_not_open_gate():
    """A stale override timestamp should not bypass the allowlist."""
    assert not gate_allows_frame(
        now_ts=300.0,
        frontmost_bundle="com.apple.mail",
        allowlist=_ALLOW,
        screen_capture_override_until=200.0,
    )


def test_override_with_no_frontmost_still_opens():
    """``frontmost_bundle=None`` is OK while an override is active."""
    assert gate_allows_frame(
        now_ts=100.0,
        frontmost_bundle=None,
        allowlist=_ALLOW,
        screen_capture_override_until=200.0,
    )


def test_empty_allowlist_only_allows_during_override():
    assert not gate_allows_frame(
        now_ts=100.0,
        frontmost_bundle="com.microsoft.teams2",
        allowlist=[],
        screen_capture_override_until=None,
    )
    assert gate_allows_frame(
        now_ts=100.0,
        frontmost_bundle="com.microsoft.teams2",
        allowlist=[],
        screen_capture_override_until=200.0,
    )


# ---------------------------------------------------------------------------
# compute_phash + hamming_distance
# ---------------------------------------------------------------------------


def test_phash_is_deterministic():
    assert compute_phash(b"hello") == compute_phash(b"hello")


def test_phash_differs_for_distinct_inputs():
    assert compute_phash(b"hello") != compute_phash(b"world")


def test_phash_length_is_eight_bytes():
    """The dedup window relies on a stable width for Hamming comparisons."""
    assert len(compute_phash(b"hello")) == 8
    assert len(compute_phash(b"")) == 8


def test_hamming_distance_zero_for_equal():
    a = compute_phash(b"x")
    assert hamming_distance(a, a) == 0


def test_hamming_distance_is_symmetric():
    a = compute_phash(b"x")
    b = compute_phash(b"y")
    assert hamming_distance(a, b) == hamming_distance(b, a)


def test_hamming_distance_handles_unequal_widths():
    """Mismatched widths are not comparable — return the upper bound."""
    assert hamming_distance(b"\x00", b"\x00\x01\x02") == 24


def test_hamming_distance_counts_bits_correctly():
    # 0xff XOR 0x00 = 0xff ⇒ 8 bits set.
    assert hamming_distance(b"\xff", b"\x00") == 8
    # 0x0f XOR 0xf0 = 0xff ⇒ 8 bits set.
    assert hamming_distance(b"\x0f", b"\xf0") == 8


# ---------------------------------------------------------------------------
# get_frontmost_bundle_id (AppKit lazy import)
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, bundle_id: str | None) -> None:
        self._bundle_id = bundle_id

    def bundleIdentifier(self) -> str | None:  # noqa: N802 — selector
        return self._bundle_id


class _FakeWorkspace:
    def __init__(self, app: _FakeApp | None) -> None:
        self._app = app

    def frontmostApplication(self) -> _FakeApp | None:  # noqa: N802 — selector
        return self._app


class _FakeWorkspaceSingleton:
    def __init__(self, workspace: _FakeWorkspace) -> None:
        self._workspace = workspace

    def sharedWorkspace(self) -> _FakeWorkspace:  # noqa: N802 — selector
        return self._workspace


def _install_fake_appkit(
    monkeypatch: pytest.MonkeyPatch,
    bundle_id: str | None,
    *,
    raise_on_lookup: Exception | None = None,
    workspace: Any = None,
) -> None:
    """Wire a fake ``AppKit`` module into ``sys.modules``.

    The real production import is ``from AppKit import NSWorkspace``;
    we stuff a stand-in module with an ``NSWorkspace`` attribute that
    behaves like the singleton-returning Objective-C class.
    """

    class _NSWorkspaceClass:
        @staticmethod
        def sharedWorkspace():  # noqa: N802 — selector
            if raise_on_lookup is not None:
                raise raise_on_lookup
            if workspace is not None:
                return workspace
            return _FakeWorkspace(
                _FakeApp(bundle_id) if bundle_id is not None else None
            )

    fake_module = types.ModuleType("AppKit")
    fake_module.NSWorkspace = _NSWorkspaceClass  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "AppKit", fake_module)


def test_frontmost_returns_bundle_id_on_happy_path(monkeypatch):
    _install_fake_appkit(monkeypatch, "com.microsoft.teams2")
    assert frontmost.get_frontmost_bundle_id() == "com.microsoft.teams2"


def test_frontmost_returns_none_when_no_app_is_frontmost(monkeypatch):
    _install_fake_appkit(
        monkeypatch,
        bundle_id=None,
        workspace=_FakeWorkspace(None),
    )
    assert frontmost.get_frontmost_bundle_id() is None


def test_frontmost_returns_none_when_app_has_no_bundle_id(monkeypatch):
    _install_fake_appkit(monkeypatch, bundle_id=None)
    assert frontmost.get_frontmost_bundle_id() is None


def test_frontmost_returns_none_when_appkit_missing(monkeypatch):
    """``sys.modules['AppKit'] = None`` triggers ImportError on import."""
    monkeypatch.setitem(sys.modules, "AppKit", None)
    assert frontmost.get_frontmost_bundle_id() is None


def test_frontmost_swallows_unexpected_exceptions(monkeypatch):
    """A PyObjC NSException should not crash the worker loop."""
    _install_fake_appkit(
        monkeypatch,
        bundle_id="com.microsoft.teams2",
        raise_on_lookup=RuntimeError("NSException-like"),
    )
    assert frontmost.get_frontmost_bundle_id() is None


def test_make_provider_with_override_returns_callable():
    override = lambda: "test-bundle"  # noqa: E731
    provider = frontmost.make_provider(override)
    assert provider() == "test-bundle"


def test_make_provider_without_override_returns_default(monkeypatch):
    _install_fake_appkit(monkeypatch, "us.zoom.xos")
    provider = frontmost.make_provider(None)
    assert provider() == "us.zoom.xos"
