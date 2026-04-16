"""macOS native notifications via osascript."""

import asyncio
import logging

from aegis.config import get_settings

logger = logging.getLogger(__name__)


async def notify(title: str, message: str, sound: bool = True) -> None:
    """Send a macOS notification via osascript.

    No-op if settings.notify_macos is False.
    Escapes quotes in title/message for AppleScript safety.
    """
    settings = get_settings()
    if not settings.notify_macos:
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
