"""Daemon-side fallback notifications via UNUserNotificationCenter.

Used when the menu bar process isn't running. The menu bar's notifier
(``helios.menubar.notifications``) handles action-button categories — those
require a long-lived delegate that survives the runloop, which only the menu
bar provides. The daemon-side notifier here is intentionally simpler:
info-only banners with no action buttons.

PyObjC is imported lazily inside method bodies so this module is importable
on non-macOS hosts (CI, Linux dev boxes) and so tests can mock the bridge
without needing PyObjC installed.

PII safety: notification ``title`` and ``body`` are user-visible content and
MUST NOT appear in log output, even at DEBUG level. Logs only carry the
notification ``identifier`` (uuid or caller-supplied) and the authorized
state.

Track 4C.1.
"""

from __future__ import annotations

import asyncio
import uuid

from helios.log import get_logger

_log = get_logger("notifications.daemon")


# UNAuthorizationStatus enum values (from <UserNotifications/UNNotificationSettings.h>):
#   0 = NotDetermined
#   1 = Denied
#   2 = Authorized
#   3 = Provisional
#   4 = Ephemeral (macOS 12+)
#
# All three of Authorized / Provisional / Ephemeral mean "we're allowed
# to post a banner". NotDetermined and Denied are the unauthorized cases.
_AUTHORIZED_STATUSES = frozenset({2, 3, 4})


def _load_user_notifications():
    """Lazy import of the UserNotifications PyObjC bridge.

    Returns the imported module. Raises ImportError on non-macOS or if the
    framework isn't installed. Callers should catch ImportError, log a
    warning, and degrade gracefully.

    Factored out so tests can monkeypatch ``sys.modules`` and exercise the
    happy path without PyObjC.
    """
    import UserNotifications  # type: ignore[import-not-found]

    return UserNotifications


class DaemonNotifier:
    """Daemon-side fallback notifier.

    Posts simple banners via ``UNUserNotificationCenter``. No action
    buttons — those are menu-bar-side only.
    """

    def __init__(self) -> None:
        # ``_authorization_requested`` flips True after a successful
        # ``requestAuthorizationWithOptions:`` call. The notification
        # center's authorization status is per-bundle in TCC but the
        # daemon's own UN process needs to call requestAuthorization
        # at least once to inherit the menu bar's grant on some macOS
        # versions; without it, ``is_authorized`` returns False even
        # when the bundle is allowed in System Settings.
        self._authorization_requested = False

    async def _request_authorization_once(self) -> None:
        """Best-effort: ask the OS to mark this process authorized.

        macOS notification authorization is keyed by bundle ID in TCC,
        but in practice each *process* in the bundle needs to call
        ``requestAuthorizationWithOptions:`` at least once or its
        ``getNotificationSettings`` returns ``notDetermined``. The
        menu bar requests at startup; the daemon mirrors it here so
        cap warnings posted by the daemon actually surface.

        Idempotent. Silently swallows any framework error — a failed
        request must not break the caller's flow.
        """
        if self._authorization_requested:
            return
        self._authorization_requested = True
        try:
            un = _load_user_notifications()
        except ImportError:
            return
        try:
            center = un.UNUserNotificationCenter.currentNotificationCenter()
        except Exception:  # noqa: BLE001
            return

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()

        def _completion(granted, error) -> None:  # noqa: ANN001
            loop.call_soon_threadsafe(_resolve, bool(granted), error)

        def _resolve(granted: bool, error) -> None:  # noqa: ANN001
            if future.done():
                return
            if error is not None:
                _log.warning(
                    "daemon_notification_authorization_failed",
                    error=str(error),
                )
            else:
                _log.info(
                    "daemon_notification_authorization_result",
                    granted=granted,
                )
            future.set_result(granted)

        try:
            # alert(1) | sound(2) | badge(4)
            center.requestAuthorizationWithOptions_completionHandler_(
                7, _completion
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "daemon_notification_authorization_request_failed",
                error=str(exc),
            )
            return
        try:
            await asyncio.wait_for(future, timeout=5.0)
        except asyncio.TimeoutError:
            _log.warning("daemon_notification_authorization_timeout")

    async def is_authorized(self) -> bool:
        """Return True if the daemon is allowed to post user notifications.

        Calls ``getNotificationSettingsWithCompletionHandler:`` and inspects
        ``authorizationStatus``. Treats ``Authorized`` and ``Provisional`` as
        authorized; everything else (NotDetermined, Denied, Ephemeral as
        applicable) is unauthorized.

        Returns False on any error (PyObjC missing, framework call fails)
        rather than raising — the caller should always be able to fall
        through.
        """
        try:
            un = _load_user_notifications()
        except ImportError as exc:
            _log.warning("usernotifications_unavailable", error=str(exc))
            return False

        try:
            center = un.UNUserNotificationCenter.currentNotificationCenter()
        except Exception as exc:  # noqa: BLE001 — framework can raise anything
            _log.warning("notification_center_unavailable", error=str(exc))
            return False

        loop = asyncio.get_running_loop()
        future: asyncio.Future[int] = loop.create_future()

        def _completion(settings) -> None:  # noqa: ANN001 — PyObjC callback
            try:
                status = int(settings.authorizationStatus())
            except Exception:  # noqa: BLE001
                status = 0
            # Hop back onto the asyncio loop to resolve the future.
            loop.call_soon_threadsafe(_resolve, status)

        def _resolve(status: int) -> None:
            if not future.done():
                future.set_result(status)

        try:
            center.getNotificationSettingsWithCompletionHandler_(_completion)
        except Exception as exc:  # noqa: BLE001
            _log.warning("get_notification_settings_failed", error=str(exc))
            return False

        try:
            status = await asyncio.wait_for(future, timeout=5.0)
        except asyncio.TimeoutError:
            _log.warning("get_notification_settings_timeout")
            return False

        authorized = status in _AUTHORIZED_STATUSES
        _log.info(
            "daemon_notification_authorization_status",
            status=status,
            authorized=authorized,
        )
        return authorized

    async def post(
        self,
        title: str,
        body: str,
        identifier: str | None = None,
    ) -> bool:
        """Enqueue a banner notification. Returns True on successful enqueue.

        Returns False if not authorized (logs a warning and skips), if
        PyObjC isn't available, or if the underlying framework call fails.

        ``title`` and ``body`` are NEVER logged.
        """
        ident = identifier if identifier is not None else str(uuid.uuid4())

        # First post in this process: tell the OS we want to post so
        # ``getNotificationSettings`` flips out of ``notDetermined``.
        await self._request_authorization_once()

        if not await self.is_authorized():
            _log.warning("notification_skipped_unauthorized", identifier=ident)
            return False

        try:
            un = _load_user_notifications()
        except ImportError as exc:
            _log.warning("usernotifications_unavailable", error=str(exc), identifier=ident)
            return False

        try:
            content = un.UNMutableNotificationContent.alloc().init()
            content.setTitle_(title)
            content.setBody_(body)

            request = un.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                ident,
                content,
                None,  # immediate delivery
            )

            center = un.UNUserNotificationCenter.currentNotificationCenter()

            loop = asyncio.get_running_loop()
            future: asyncio.Future[bool] = loop.create_future()

            def _completion(error) -> None:  # noqa: ANN001 — PyObjC callback
                ok = error is None
                loop.call_soon_threadsafe(_resolve, ok, error)

            def _resolve(ok: bool, error) -> None:  # noqa: ANN001
                if future.done():
                    return
                if not ok:
                    # The error object is an NSError — stringifying is safe
                    # (no PII, just framework-level text).
                    _log.warning(
                        "notification_post_failed",
                        identifier=ident,
                        error=str(error),
                    )
                future.set_result(ok)

            center.addNotificationRequest_withCompletionHandler_(request, _completion)

            try:
                ok = await asyncio.wait_for(future, timeout=5.0)
            except asyncio.TimeoutError:
                _log.warning("notification_post_timeout", identifier=ident)
                return False

            if ok:
                _log.info("notification_posted", identifier=ident)
            return ok
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "notification_post_failed",
                identifier=ident,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False


async def notify(
    title: str,
    body: str,
    identifier: str | None = None,
) -> bool:
    """Module-level convenience wrapper around :class:`DaemonNotifier`.

    Equivalent to ``await DaemonNotifier().post(title, body, identifier)``.
    Use this when you don't want to hold a notifier instance.
    """
    return await DaemonNotifier().post(title, body, identifier)
