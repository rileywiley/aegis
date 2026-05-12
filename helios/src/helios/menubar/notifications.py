"""Menu-bar-side notifier with action-button categories.

Used from the menu bar process (rumps app). Wraps ``UNUserNotificationCenter``
and registers a fixed set of categories so notifications can carry action
buttons (Continue / Stop, Open Settings, Retry, etc). When a user clicks a
button, macOS invokes our delegate, which looks up the registered Python
callback and fires it.

Why this lives separately from the daemon notifier:
  * The daemon notifier (``helios.notifications.notify``) is async and
    info-only — no buttons. It runs inside an asyncio runloop.
  * Action buttons require a long-lived ``UNUserNotificationCenterDelegate``
    that survives between post and click. Only the menu bar process has
    that lifetime; the daemon's tasks come and go. Hence: this module is
    sync (rumps runloop is sync) and registers the delegate once.

PyObjC is imported lazily inside method bodies so this module imports on
non-macOS hosts (CI, Linux dev boxes) and so tests can mock the bridge by
injecting a fake ``UserNotifications`` module into ``sys.modules``.

PII safety: notification ``title`` and ``body`` are user-visible content
and MUST NOT appear in log output, even at DEBUG level. Logs only carry the
notification ``identifier`` (uuid or caller-supplied), the category, and
authorized state. Action callbacks may receive ``user_info`` payloads from
the post site — that payload is forwarded as-is to the callback but is
never logged.

Track 4C.2.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Callable

from helios.log import get_logger

_log = get_logger("notifications.menubar")


# UNAuthorizationStatus enum values — see ``helios.notifications.notify``
# for the same comment. Apple's headers define Authorized=2, Provisional=3,
# Ephemeral=4 (macOS 12+); all three permit posting banners.
_AUTHORIZED_STATUSES = frozenset({2, 3, 4})


# UNNotificationActionOptions: 0 = no special options. We keep all action
# buttons "foreground" (the default) by passing 0 — clicking the button
# brings the app to the foreground briefly so the delegate can run, which
# is fine for our use cases.
_ACTION_OPTION_NONE = 0
_CATEGORY_OPTION_NONE = 0


# ---------------------------------------------------------------------------
# Category & action definitions (per HELIOS.md §14.1 + build plan §4C)
# ---------------------------------------------------------------------------


def _action_titles() -> dict[str, str]:
    """Human-readable button titles for each action identifier.

    Kept in one place so we can change wording without hunting through
    category definitions. Values are user-visible strings — they show up
    in the OS notification and never appear in logs.
    """
    return {
        "continue": "Continue",
        "stop": "Stop",
        "open_settings": "Open Settings",
        "open_save_window": "Open Save Window",
        "retry": "Retry",
        "open_accessibility_settings": "Open Accessibility Settings",
    }


def _category_definitions() -> list[tuple[str, list[str]]]:
    """Return ``(category_id, [action_id, ...])`` tuples in registration order.

    Categories with an empty action list are still registered so that
    ``post()`` can be called with that category — the resulting notification
    is just a banner with no buttons.
    """
    return [
        ("FOUR_HOUR_PROMPT", ["continue", "stop"]),
        ("PERMISSION_REVOKED", ["open_settings"]),
        ("MEETING_INTERRUPTED", ["open_settings"]),
        ("MISSED_MEETING", ["open_settings"]),
        ("MANUAL_SCREEN_ENDED", []),
        ("CONTINUOUS_AUTO_STOPPED", []),
        ("VOICE_NOTE_CAP_WARNING", ["stop", "continue"]),
        ("VOICE_NOTE_CAP_REACHED", ["open_save_window"]),
        ("VOICE_NOTE_SAVE_FAILED", ["retry"]),
        ("VOICE_NOTE_HOTKEY_FAILED", ["open_accessibility_settings"]),
    ]


_KNOWN_CATEGORIES = frozenset(cat for cat, _ in _category_definitions())


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


# ---------------------------------------------------------------------------
# Delegate
# ---------------------------------------------------------------------------


class _ResponseDelegate:
    """Receives action-button clicks and dispatches to the callback registry.

    On real macOS this would be an Objective-C subclass of NSObject
    implementing ``UNUserNotificationCenterDelegate``. We only need the
    one method (``userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:``);
    PyObjC infers the protocol from the selector name when we set the
    delegate via ``setDelegate_``.

    To keep the module importable without PyObjC, this is a plain Python
    class. PyObjC tolerates plain Python objects as delegates — it inspects
    method names. The selector below uses underscores in place of colons,
    which is the standard PyObjC translation rule.

    The class is intentionally simple and stateless apart from the registry
    reference, so tests can instantiate it directly and call the selector
    with fake response objects.
    """

    def __init__(
        self,
        registry: dict[tuple[str, str], Callable[[dict], None]],
    ) -> None:
        # Hold a reference to the live registry dict from the notifier so
        # later ``on_action`` calls take effect without re-installing the
        # delegate.
        self._registry = registry

    # PyObjC selector mapping:
    #   ``userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:``
    #   becomes
    #   ``userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_``.
    def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(  # noqa: N802
        self,
        center: Any,
        response: Any,
        completion_handler: Callable[[], None] | None,
    ) -> None:
        try:
            self._dispatch(response)
        finally:
            # Always call the completion handler so macOS knows we're done.
            if completion_handler is not None:
                try:
                    completion_handler()
                except Exception as exc:  # noqa: BLE001 — defensive
                    _log.warning(
                        "notification_completion_handler_failed",
                        error=str(exc),
                    )

    def _dispatch(self, response: Any) -> None:
        try:
            notification = response.notification()
            request = notification.request()
            content = request.content()
            category = str(content.categoryIdentifier())
            action = str(response.actionIdentifier())
            # ``userInfo`` is an NSDictionary; PyObjC bridges it to a dict.
            user_info_raw = content.userInfo()
            user_info: dict = dict(user_info_raw) if user_info_raw is not None else {}
            identifier = str(request.identifier())
        except Exception as exc:  # noqa: BLE001
            _log.warning("notification_response_parse_failed", error=str(exc))
            return

        # macOS sends a synthetic ``actionIdentifier`` of
        # ``UNNotificationDefaultActionIdentifier`` (the user tapped the
        # banner body itself) or ``UNNotificationDismissActionIdentifier``
        # (the user dismissed). We don't have callbacks for those — silently
        # ignore unless a custom action matches.
        cb = self._registry.get((category, action))
        if cb is None:
            _log.debug(
                "notification_action_no_callback",
                category=category,
                action=action,
                identifier=identifier,
            )
            return

        try:
            cb(user_info)
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning(
                "notification_callback_failed",
                category=category,
                action=action,
                identifier=identifier,
                error=str(exc),
                error_type=type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# MenuBarNotifier
# ---------------------------------------------------------------------------


class MenuBarNotifier:
    """Menu-bar-side notifier.

    Registers the fixed set of notification categories (with action
    buttons) at startup, posts notifications with a category, and
    dispatches action-button clicks to per-(category, action) callbacks.

    All methods are synchronous — the rumps runloop driving the menu
    bar app is sync, so async would force a loop-bridge dance. Use the
    daemon notifier (``helios.notifications.notify``) for asyncio
    contexts.
    """

    def __init__(self) -> None:
        # ``_callbacks`` is keyed by (category, action) and maps to the
        # most recently registered callback. The delegate holds a reference
        # to this same dict so subsequent ``on_action`` calls take effect
        # without re-installing the delegate.
        self._callbacks: dict[tuple[str, str], Callable[[dict], None]] = {}
        # ``_delegate`` is created on first ``register_categories`` call and
        # kept alive on the instance (PyObjC delegates are weakly held by
        # the framework; if we drop our reference, the delegate is GCed and
        # callbacks vanish).
        self._delegate: _ResponseDelegate | None = None
        # ``_categories_registered`` flips True after a successful
        # ``register_categories`` so we don't redundantly recreate
        # category objects on a second call. The framework call is
        # idempotent on macOS, but skipping it on the second call keeps
        # logs quieter.
        self._categories_registered = False

    # -- public API -------------------------------------------------------

    def request_authorization(self) -> None:
        """Prompt macOS for notification permission (alert + sound).

        Without this call, ``UNUserNotificationCenter`` keeps the app's
        authorization status at ``notDetermined`` (0), and every
        ``post()`` quietly skips because ``is_authorized()`` returns
        False. The daemon shares this bundle ID, so once the menu bar
        gets authorization the daemon's cap-warning posts surface too.

        Idempotent. Safe to call before/after ``register_categories``.
        Logs and swallows any framework error — the menu bar must not
        fail to launch if UserNotifications is missing or denied.
        """
        try:
            un = _load_user_notifications()
        except ImportError as exc:
            _log.warning("usernotifications_unavailable", error=str(exc))
            return
        try:
            # UNAuthorizationOptions: alert(1) | sound(2) | badge(4) = 7.
            opts = 1 | 2 | 4
            center = un.UNUserNotificationCenter.currentNotificationCenter()

            def _completion(granted, error) -> None:  # noqa: ANN001
                if error is not None:
                    _log.warning(
                        "notification_authorization_failed",
                        error=str(error),
                    )
                else:
                    _log.info(
                        "notification_authorization_result",
                        granted=bool(granted),
                    )

            center.requestAuthorizationWithOptions_completionHandler_(
                opts, _completion
            )
        except Exception as exc:  # noqa: BLE001 — framework can raise anything
            _log.warning(
                "notification_authorization_request_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def register_categories(self) -> None:
        """Register all notification categories and install the delegate.

        Idempotent — calling twice does not duplicate categories.

        Silently returns (logs a warning) if PyObjC isn't available — this
        lets the menu bar still launch on hosts where notifications won't
        work, rather than hard-failing at startup.
        """
        # Authorization must be requested at least once so subsequent
        # ``post()`` calls aren't silently dropped. Idempotent on the OS
        # side (macOS only prompts the first time).
        self.request_authorization()

        try:
            un = _load_user_notifications()
        except ImportError as exc:
            _log.warning("usernotifications_unavailable", error=str(exc))
            return

        try:
            categories = []
            for category_id, action_ids in _category_definitions():
                actions = [
                    self._make_action(un, action_id) for action_id in action_ids
                ]
                category = self._make_category(un, category_id, actions)
                categories.append(category)

            center = un.UNUserNotificationCenter.currentNotificationCenter()

            # Install the delegate first so it's in place before any
            # categories fire. The delegate holds a live reference to our
            # callback registry.
            if self._delegate is None:
                self._delegate = _ResponseDelegate(self._callbacks)
            try:
                center.setDelegate_(self._delegate)
            except Exception as exc:  # noqa: BLE001
                _log.warning("notification_delegate_install_failed", error=str(exc))

            # Replace the category set wholesale. Apple's API takes an
            # NSSet, but PyObjC bridges Python sequences acceptably; tests
            # mock at this level.
            center.setNotificationCategories_(set(categories))

            self._categories_registered = True
            _log.info(
                "notification_categories_registered",
                count=len(categories),
            )
        except Exception as exc:  # noqa: BLE001 — framework can raise anything
            _log.warning(
                "notification_categories_register_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def post(
        self,
        category: str,
        title: str,
        body: str,
        identifier: str | None = None,
        user_info: dict | None = None,
    ) -> bool:
        """Enqueue a notification under ``category``.

        Returns True on successful enqueue, False otherwise. False covers:
            * unknown category (logs a warning, does not raise)
            * not authorized
            * PyObjC unavailable
            * framework error during the post call

        Unknown categories return False rather than raising so a buggy
        caller cannot crash the menu bar runloop. The build plan's wiring
        layer is responsible for using only known categories; this method
        is the safety net.

        ``title`` and ``body`` are NEVER logged.
        ``user_info`` is forwarded into the notification's ``userInfo``
        and surfaced to the action callback when the user clicks a
        button. It must be a JSON-friendly dict (str keys, primitive
        values) — PyObjC will reject anything else.
        """
        ident = identifier if identifier is not None else str(uuid.uuid4())

        if category not in _KNOWN_CATEGORIES:
            _log.warning(
                "notification_unknown_category",
                category=category,
                identifier=ident,
            )
            return False

        if not self.is_authorized():
            _log.warning(
                "notification_skipped_unauthorized",
                category=category,
                identifier=ident,
            )
            return False

        try:
            un = _load_user_notifications()
        except ImportError as exc:
            _log.warning(
                "usernotifications_unavailable",
                error=str(exc),
                identifier=ident,
            )
            return False

        try:
            content = un.UNMutableNotificationContent.alloc().init()
            content.setTitle_(title)
            content.setBody_(body)
            content.setCategoryIdentifier_(category)
            if user_info:
                # Keep a defensive copy so subsequent caller mutations
                # don't reach into the framework.
                content.setUserInfo_(dict(user_info))

            request = un.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                ident,
                content,
                None,  # immediate delivery
            )

            center = un.UNUserNotificationCenter.currentNotificationCenter()

            # Sync API: the framework's add-request call accepts a
            # completion handler, but we don't await it. We block briefly
            # on a threading.Event so we can surface enqueue failures to
            # the caller, then return.
            done = threading.Event()
            result: dict[str, Any] = {"ok": True, "error": None}

            def _completion(error: Any) -> None:
                if error is not None:
                    result["ok"] = False
                    result["error"] = error
                done.set()

            center.addNotificationRequest_withCompletionHandler_(request, _completion)

            # Wait briefly for the framework to process the enqueue. 5s is
            # generous — addNotificationRequest is local-only, no network.
            if not done.wait(timeout=5.0):
                _log.warning("notification_post_timeout", identifier=ident)
                return False

            if not result["ok"]:
                _log.warning(
                    "notification_post_failed",
                    identifier=ident,
                    category=category,
                    error=str(result["error"]),
                )
                return False

            _log.info(
                "notification_posted",
                identifier=ident,
                category=category,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "notification_post_failed",
                identifier=ident,
                category=category,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False

    def on_action(
        self,
        category: str,
        action: str,
        callback: Callable[[dict], None],
    ) -> None:
        """Register a callback for a ``(category, action)`` button click.

        The callback receives the ``user_info`` dict that was passed to
        ``post()`` (an empty dict if none was supplied). It is invoked
        synchronously on whatever thread macOS uses to deliver the
        notification response — typically the main thread inside the
        rumps runloop.

        Re-registering overwrites any prior callback for the same key.
        """
        self._callbacks[(category, action)] = callback

    def is_authorized(self) -> bool:
        """Return True if posting notifications is currently allowed.

        Synchronous: blocks for up to 5s on the framework's settings
        callback. Returns False on any error (PyObjC missing, timeout,
        framework call fails) — the caller should always be able to fall
        through.
        """
        try:
            un = _load_user_notifications()
        except ImportError as exc:
            _log.warning("usernotifications_unavailable", error=str(exc))
            return False

        try:
            center = un.UNUserNotificationCenter.currentNotificationCenter()
        except Exception as exc:  # noqa: BLE001
            _log.warning("notification_center_unavailable", error=str(exc))
            return False

        done = threading.Event()
        status_holder: dict[str, int] = {"status": 0}

        def _completion(settings: Any) -> None:
            try:
                status_holder["status"] = int(settings.authorizationStatus())
            except Exception:  # noqa: BLE001
                status_holder["status"] = 0
            done.set()

        try:
            center.getNotificationSettingsWithCompletionHandler_(_completion)
        except Exception as exc:  # noqa: BLE001
            _log.warning("get_notification_settings_failed", error=str(exc))
            return False

        if not done.wait(timeout=5.0):
            _log.warning("get_notification_settings_timeout")
            return False

        status = status_holder["status"]
        authorized = status in _AUTHORIZED_STATUSES
        _log.debug(
            "notification_authorization_status",
            status=status,
            authorized=authorized,
        )
        return authorized

    # -- internal helpers -------------------------------------------------

    @staticmethod
    def _make_action(un: Any, action_id: str) -> Any:
        """Build a UNNotificationAction from our action_id string."""
        title = _action_titles().get(action_id, action_id)
        return un.UNNotificationAction.actionWithIdentifier_title_options_(
            action_id,
            title,
            _ACTION_OPTION_NONE,
        )

    @staticmethod
    def _make_category(un: Any, category_id: str, actions: list[Any]) -> Any:
        """Build a UNNotificationCategory from id + action list."""
        # ``intentIdentifiers`` is a list (possibly empty) of intent class
        # names for Siri integration; we don't use Siri intents, so [].
        return un.UNNotificationCategory.categoryWithIdentifier_actions_intentIdentifiers_options_(
            category_id,
            actions,
            [],
            _CATEGORY_OPTION_NONE,
        )
