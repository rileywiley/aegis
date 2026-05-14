"""Frontmost-application bundle-id provider — Track 5A.

The OCR worker only runs when the macOS frontmost application is in the
configured meeting-app allowlist (HELIOS.md §10). This module wraps the
``NSWorkspace`` PyObjC call and exposes a tiny, easily-mocked interface so
the worker stays portable / testable.

Lazy-import policy mirrors :mod:`helios.menubar.notifications`: the PyObjC
bridge is imported inside the function body so the module loads on
non-macOS hosts (CI, dev Linux boxes). Tests inject a fake ``AppKit``
module into ``sys.modules`` to drive the happy path without PyObjC.

PII safety: bundle identifiers are not user content — they are stable
reverse-DNS strings published in each app's ``Info.plist`` — so logging
them at DEBUG level is allowed. The window TITLE is *not* read by this
module; we only return the app's bundle id.
"""

from __future__ import annotations

from typing import Callable, Protocol

from helios.log import get_logger

_log = get_logger("workers.frontmost")


class FrontmostProvider(Protocol):
    """Callable contract: return the frontmost app's bundle id, or ``None``.

    ``None`` is returned when (a) we're not on macOS, (b) the PyObjC
    bridge isn't installed, or (c) no application is frontmost at the
    instant we asked (briefly possible during Dock activation /
    Mission Control switches).
    """

    def __call__(self) -> str | None: ...


def get_frontmost_bundle_id() -> str | None:
    """Return the frontmost macOS application's bundle id, or ``None``.

    Lazy-imports ``AppKit.NSWorkspace`` so non-macOS hosts can still
    import this module. Any failure (no AppKit, no frontmost app, no
    bundle id) is swallowed and returned as ``None`` — the caller (the
    OCR gating check) treats ``None`` as "do not OCR".
    """
    try:
        # Lazy import — module loads on Linux CI without this present.
        # NSWorkspace is the canonical API; the singleton is shared
        # across the process.
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        workspace = NSWorkspace.sharedWorkspace()
        app = workspace.frontmostApplication()
        if app is None:
            return None
        bundle = app.bundleIdentifier()
        if bundle is None:
            return None
        return str(bundle)
    except Exception as exc:  # noqa: BLE001 — defensive
        # PyObjC can raise opaque NSExceptions on some bridge errors.
        # Log + return None rather than killing the worker loop.
        _log.warning("frontmost_lookup_failed", error=str(exc))
        return None


def make_provider(
    override: Callable[[], str | None] | None = None,
) -> FrontmostProvider:
    """Return a provider callable.

    ``override`` is for tests / non-mac runtimes: pass a callable that
    returns a canned bundle id and we'll use it instead of the real
    PyObjC path. Pass ``None`` to get the real implementation.
    """
    if override is not None:
        return override
    return get_frontmost_bundle_id
