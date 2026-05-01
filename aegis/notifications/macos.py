"""macOS native notifications via osascript."""

import asyncio
import logging

from aegis.config import get_settings
from aegis.db.admin_config import _admin_overrides

logger = logging.getLogger(__name__)


def _is_notify_enabled() -> bool:
    """Check notify_macos from admin overrides first, then config default."""
    # Check in-memory admin override cache (written by admin panel saves)
    if "notify_macos" in _admin_overrides:
        val = _admin_overrides["notify_macos"]
        # Handle JSONB dict formats
        if isinstance(val, dict):
            val = val.get("v", val.get("value", val))
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() not in ("false", "0", "no")
        return bool(val)
    return get_settings().notify_macos


async def notify(title: str, message: str, sound: bool = True) -> None:
    """Send a macOS notification via osascript.

    No-op if notify_macos is disabled in admin settings.
    Escapes quotes in title/message for AppleScript safety.
    """
    if not _is_notify_enabled():
        return

    # Escape double quotes and backslashes for AppleScript string literals
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{safe_message}" with title "{safe_title}"'
    if sound:
        script += ' sound name "default"'

    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "osascript notification failed (rc=%d): %s",
                proc.returncode,
                stderr.decode().strip()[:200],
            )
    except FileNotFoundError:
        logger.warning("osascript not found — macOS notifications unavailable")
    except Exception:
        logger.exception("Failed to send macOS notification")
