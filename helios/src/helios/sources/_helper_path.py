"""Resolve the bundled ScreenCaptureHelper binary path in dev or py2app bundle."""

from __future__ import annotations

import os
from pathlib import Path

_HELPER_NAME = "ScreenCaptureHelper"


def get_helper_path() -> Path:
    """Locate the Swift ScreenCaptureHelper binary.

    py2app sets RESOURCEPATH to Contents/Resources/ inside the bundle; in dev
    the binary lives at helios/bin/ScreenCaptureHelper.
    """
    resource_path = os.environ.get("RESOURCEPATH")
    if resource_path:
        return Path(resource_path) / "bin" / _HELPER_NAME
    return Path(__file__).resolve().parents[3] / "bin" / _HELPER_NAME
