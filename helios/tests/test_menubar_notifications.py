"""Tests for the menu-bar-side notifier (``helios.menubar.notifications``).

These tests must run on any host, including non-macOS CI. The PyObjC
``UserNotifications`` framework is mocked by injecting a fake module into
``sys.modules`` before the notifier loads it. The fakes mirror the API
surface that ``notifications.py`` actually touches; if you find yourself
reaching for an attribute that the fake doesn't expose, it's likely a
sign the production code is doing something not yet covered here.

Track 4C.2.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any

import pytest

from helios.menubar import notifications as notif_module
from helios.menubar.notifications import (
    MenuBarNotifier,
    _ResponseDelegate,
    _category_definitions,
)


# ---------------------------------------------------------------------------
# Fake UserNotifications framework
# ---------------------------------------------------------------------------


class _FakeSettings:
    def __init__(self, status: int) -> None:
        self._status = status

    def authorizationStatus(self) -> int:
        return self._status


class _FakeContent:
    """Stand-in for ``UNMutableNotificationContent``."""

    def __init__(self) -> None:
        self.title: str | None = None
        self.body: str | None = None
        self.category_identifier: str | None = None
        self.user_info: dict | None = None

    @classmethod
    def alloc(cls) -> "_FakeContent":
        return cls()

    def init(self) -> "_FakeContent":
        return self

    def setTitle_(self, title: str) -> None:
        self.title = title

    def setBody_(self, body: str) -> None:
        self.body = body

    def setCategoryIdentifier_(self, category: str) -> None:
        self.category_identifier = category

    def setUserInfo_(self, info: dict) -> None:
        self.user_info = info

    # Read-side accessors for the delegate
    def categoryIdentifier(self) -> str | None:
        return self.category_identifier

    def userInfo(self) -> dict | None:
        return self.user_info


class _FakeRequest:
    """Stand-in for ``UNNotificationRequest``."""

    def __init__(self, identifier: str, content: _FakeContent, trigger: Any) -> None:
        self._identifier = identifier
        self._content = content
        self._trigger = trigger

    @classmethod
    def requestWithIdentifier_content_trigger_(
        cls, identifier: str, content: _FakeContent, trigger: Any
    ) -> "_FakeRequest":
        return cls(identifier, content, trigger)

    # The delegate calls these as accessors.
    def identifier(self) -> str:
        return self._identifier

    def content(self) -> _FakeContent:
        return self._content


class _FakeAction:
    def __init__(self, identifier: str, title: str, options: int) -> None:
        self.identifier = identifier
        self.title = title
        self.options = options

    @classmethod
    def actionWithIdentifier_title_options_(
        cls, identifier: str, title: str, options: int
    ) -> "_FakeAction":
        return cls(identifier, title, options)


class _FakeCategory:
    def __init__(
        self,
        identifier: str,
        actions: list[_FakeAction],
        intent_identifiers: list[str],
        options: int,
    ) -> None:
        self.identifier = identifier
        self.actions = list(actions)
        self.intent_identifiers = list(intent_identifiers)
        self.options = options

    @classmethod
    def categoryWithIdentifier_actions_intentIdentifiers_options_(
        cls,
        identifier: str,
        actions: list[_FakeAction],
        intent_identifiers: list[str],
        options: int,
    ) -> "_FakeCategory":
        return cls(identifier, actions, intent_identifiers, options)

    # Make _FakeCategory hashable so it can live in a Python set (the
    # production code wraps the list in ``set(...)`` before handing to
    # setNotificationCategories_).
    def __hash__(self) -> int:
        return hash(self.identifier)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeCategory) and other.identifier == self.identifier


class _FakeCenter:
    """Stand-in for ``UNUserNotificationCenter`` instance."""

    def __init__(
        self,
        status: int = 3,
        post_error: Any = None,
        async_callbacks: bool = True,
    ) -> None:
        self._status = status
        self._post_error = post_error
        self._async_callbacks = async_callbacks
        self.posted_requests: list[_FakeRequest] = []
        self.registered_categories: set[_FakeCategory] = set()
        self.delegate: Any = None

    def setDelegate_(self, delegate: Any) -> None:
        self.delegate = delegate

    def setNotificationCategories_(self, categories: Any) -> None:
        # Production passes a Python set; record the latest snapshot.
        self.registered_categories = set(categories)

    def getNotificationSettingsWithCompletionHandler_(self, completion) -> None:
        settings = _FakeSettings(self._status)
        if self._async_callbacks:
            # Real PyObjC fires completion on a background thread. Simulate
            # that to make sure the threading.Event blocking works.
            threading.Thread(
                target=lambda: completion(settings), daemon=True
            ).start()
        else:
            completion(settings)

    def addNotificationRequest_withCompletionHandler_(
        self, request: _FakeRequest, completion
    ) -> None:
        self.posted_requests.append(request)
        err = self._post_error
        if self._async_callbacks:
            threading.Thread(target=lambda: completion(err), daemon=True).start()
        else:
            completion(err)


class _FakeUserNotifications:
    """Module-shaped fake exposing the symbols ``notifications.py`` imports."""

    def __init__(self, status: int = 3, post_error: Any = None) -> None:
        self._center = _FakeCenter(status=status, post_error=post_error)
        center = self._center

        class _UNCenterShim:
            @staticmethod
            def currentNotificationCenter() -> _FakeCenter:
                return center

        self.UNUserNotificationCenter = _UNCenterShim
        self.UNMutableNotificationContent = _FakeContent
        self.UNNotificationRequest = _FakeRequest
        self.UNNotificationAction = _FakeAction
        self.UNNotificationCategory = _FakeCategory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def install_fake_un(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``UserNotifications`` module into ``sys.modules``.

    Returns a factory: ``install(status=3, post_error=None) -> _FakeUserNotifications``.
    """

    def install(status: int = 3, post_error: Any = None) -> _FakeUserNotifications:
        fake = _FakeUserNotifications(status=status, post_error=post_error)
        monkeypatch.setitem(sys.modules, "UserNotifications", fake)  # type: ignore[arg-type]
        return fake

    return install


@pytest.fixture
def remove_un(monkeypatch: pytest.MonkeyPatch):
    """Make ``import UserNotifications`` raise ImportError."""
    monkeypatch.setitem(sys.modules, "UserNotifications", None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helpers for building fake notification responses
# ---------------------------------------------------------------------------


class _FakeNotification:
    def __init__(self, request: _FakeRequest) -> None:
        self._request = request

    def request(self) -> _FakeRequest:
        return self._request


class _FakeResponse:
    def __init__(self, notification: _FakeNotification, action_identifier: str) -> None:
        self._notification = notification
        self._action_identifier = action_identifier

    def notification(self) -> _FakeNotification:
        return self._notification

    def actionIdentifier(self) -> str:
        return self._action_identifier


def _build_response(
    category: str,
    action: str,
    user_info: dict | None = None,
    identifier: str = "test-notif",
) -> _FakeResponse:
    """Build a fake response object matching what macOS would deliver."""
    content = _FakeContent.alloc().init()
    content.setCategoryIdentifier_(category)
    if user_info is not None:
        content.setUserInfo_(user_info)
    request = _FakeRequest(identifier, content, None)
    return _FakeResponse(_FakeNotification(request), action)


# ---------------------------------------------------------------------------
# Tests — register_categories
# ---------------------------------------------------------------------------


def test_register_categories_registers_all_ten(install_fake_un) -> None:
    """Case 1: register_categories() registers exactly 10 categories with
    the correct action button counts per HELIOS.md §14.1."""
    fake = install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()

    registered = fake._center.registered_categories
    assert len(registered) == 10

    by_id = {cat.identifier: cat for cat in registered}

    expected = {
        "FOUR_HOUR_PROMPT": ["continue", "stop"],
        "PERMISSION_REVOKED": ["open_settings"],
        "MEETING_INTERRUPTED": ["open_settings"],
        "MISSED_MEETING": ["open_settings"],
        "MANUAL_SCREEN_ENDED": [],
        "CONTINUOUS_AUTO_STOPPED": [],
        "VOICE_NOTE_CAP_WARNING": ["stop", "continue"],
        "VOICE_NOTE_CAP_REACHED": ["open_save_window"],
        "VOICE_NOTE_SAVE_FAILED": ["retry"],
        "VOICE_NOTE_HOTKEY_FAILED": ["open_accessibility_settings"],
    }
    assert set(by_id.keys()) == set(expected.keys())

    for cat_id, action_ids in expected.items():
        cat = by_id[cat_id]
        actual_action_ids = [a.identifier for a in cat.actions]
        assert actual_action_ids == action_ids, (
            f"{cat_id}: expected actions {action_ids}, got {actual_action_ids}"
        )


def test_register_categories_idempotent(install_fake_un) -> None:
    """Case 2: register_categories() called twice — still 10 categories,
    no duplicates."""
    fake = install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()
    notifier.register_categories()

    assert len(fake._center.registered_categories) == 10
    # Identifiers unique
    ids = [cat.identifier for cat in fake._center.registered_categories]
    assert len(ids) == len(set(ids))


def test_register_categories_installs_delegate(install_fake_un) -> None:
    """register_categories() installs a delegate on the center."""
    fake = install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()

    assert fake._center.delegate is not None
    assert isinstance(fake._center.delegate, _ResponseDelegate)


# ---------------------------------------------------------------------------
# Tests — post
# ---------------------------------------------------------------------------


def test_post_known_category(install_fake_un) -> None:
    """Case 3: post() with a known category posts a notification carrying
    that category."""
    fake = install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()
    result = notifier.post(
        category="FOUR_HOUR_PROMPT",
        title="Still capturing",
        body="Continue or stop?",
        identifier="prompt-1",
    )

    assert result is True
    assert len(fake._center.posted_requests) == 1
    request = fake._center.posted_requests[0]
    assert request.identifier() == "prompt-1"
    assert request.content().title == "Still capturing"
    assert request.content().body == "Continue or stop?"
    assert request.content().categoryIdentifier() == "FOUR_HOUR_PROMPT"


def test_post_unknown_category_returns_false(
    install_fake_un, caplog: pytest.LogCaptureFixture
) -> None:
    """Case 4: post() with an unknown category returns False, logs a
    warning, and does not raise. Behavior documented in the post() docstring.
    """
    fake = install_fake_un(status=3)
    caplog.set_level(logging.WARNING)

    notifier = MenuBarNotifier()
    notifier.register_categories()
    result = notifier.post(
        category="NOT_A_REAL_CATEGORY",
        title="t",
        body="b",
    )

    assert result is False
    # Nothing was posted to the framework
    assert fake._center.posted_requests == []


def test_post_unauthorized_returns_false(install_fake_un) -> None:
    """post() returns False when status is unauthorized (e.g. Denied=1)."""
    fake = install_fake_un(status=1)

    notifier = MenuBarNotifier()
    notifier.register_categories()
    result = notifier.post(
        category="MANUAL_SCREEN_ENDED",
        title="Capture ended",
        body="Saved",
    )

    assert result is False
    assert fake._center.posted_requests == []


def test_post_default_identifier_is_uuid(install_fake_un) -> None:
    """When no identifier is supplied, a UUID is generated."""
    fake = install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()
    notifier.post(category="MANUAL_SCREEN_ENDED", title="t", body="b")

    assert len(fake._center.posted_requests) == 1
    ident = fake._center.posted_requests[0].identifier()
    assert isinstance(ident, str)
    assert len(ident) == 36
    assert ident.count("-") == 4


def test_post_carries_user_info(install_fake_un) -> None:
    """user_info passed to post() lands on the request content."""
    fake = install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()
    notifier.post(
        category="VOICE_NOTE_SAVE_FAILED",
        title="Save failed",
        body="Could not write file",
        identifier="save-1",
        user_info={"voice_note_id": "abc-123", "reason": "disk_full"},
    )

    request = fake._center.posted_requests[0]
    assert request.content().userInfo() == {
        "voice_note_id": "abc-123",
        "reason": "disk_full",
    }


# ---------------------------------------------------------------------------
# Tests — on_action + delegate dispatch
# ---------------------------------------------------------------------------


def test_on_action_registers_callback(install_fake_un) -> None:
    """Case 5: on_action() stores the callback for (category, action)."""
    install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()

    cb_calls: list[dict] = []

    def cb(user_info: dict) -> None:
        cb_calls.append(user_info)

    notifier.on_action("FOUR_HOUR_PROMPT", "continue", cb)

    assert ("FOUR_HOUR_PROMPT", "continue") in notifier._callbacks
    assert notifier._callbacks[("FOUR_HOUR_PROMPT", "continue")] is cb


def test_delegate_dispatches_continue(install_fake_un) -> None:
    """Case 6: simulate user clicking Continue on FOUR_HOUR_PROMPT;
    callback fires with user_info."""
    install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()

    received: list[dict] = []

    def on_continue(user_info: dict) -> None:
        received.append(user_info)

    notifier.on_action("FOUR_HOUR_PROMPT", "continue", on_continue)

    response = _build_response(
        "FOUR_HOUR_PROMPT",
        "continue",
        user_info={"session_id": "s-42"},
    )

    completion_called = []

    def completion() -> None:
        completion_called.append(True)

    notifier._delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        None,
        response,
        completion,
    )

    assert received == [{"session_id": "s-42"}]
    # Completion handler must always be invoked
    assert completion_called == [True]


def test_delegate_dispatches_different_callbacks_per_action(install_fake_un) -> None:
    """Case 7: delegate dispatches Continue and Stop on the same category
    to different callbacks."""
    install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()

    continue_calls: list[dict] = []
    stop_calls: list[dict] = []

    notifier.on_action(
        "FOUR_HOUR_PROMPT", "continue", lambda info: continue_calls.append(info)
    )
    notifier.on_action(
        "FOUR_HOUR_PROMPT", "stop", lambda info: stop_calls.append(info)
    )

    notifier._delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        None,
        _build_response("FOUR_HOUR_PROMPT", "continue", {"who": "continue"}),
        lambda: None,
    )
    notifier._delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        None,
        _build_response("FOUR_HOUR_PROMPT", "stop", {"who": "stop"}),
        lambda: None,
    )

    assert continue_calls == [{"who": "continue"}]
    assert stop_calls == [{"who": "stop"}]


def test_delegate_unknown_action_does_not_raise(install_fake_un) -> None:
    """Delegate silently ignores responses for which no callback is
    registered (e.g. default-action / dismiss)."""
    install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()

    completion_called = []

    notifier._delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        None,
        _build_response("FOUR_HOUR_PROMPT", "com.apple.UNNotificationDefaultActionIdentifier"),
        lambda: completion_called.append(True),
    )

    # No exception, completion still called
    assert completion_called == [True]


def test_on_action_overwrites_previous_callback(install_fake_un) -> None:
    """Re-registering on_action() replaces the prior callback."""
    install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()

    first_calls: list[dict] = []
    second_calls: list[dict] = []

    notifier.on_action("VOICE_NOTE_SAVE_FAILED", "retry", lambda i: first_calls.append(i))
    notifier.on_action("VOICE_NOTE_SAVE_FAILED", "retry", lambda i: second_calls.append(i))

    notifier._delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        None,
        _build_response("VOICE_NOTE_SAVE_FAILED", "retry", {"vn": "x"}),
        lambda: None,
    )

    assert first_calls == []
    assert second_calls == [{"vn": "x"}]


def test_callback_exception_does_not_crash_delegate(install_fake_un) -> None:
    """A callback that raises must not propagate out of the delegate."""
    install_fake_un(status=3)

    notifier = MenuBarNotifier()
    notifier.register_categories()

    def raising_cb(_user_info: dict) -> None:
        raise RuntimeError("boom")

    notifier.on_action("PERMISSION_REVOKED", "open_settings", raising_cb)

    completion_called = []

    # Should not raise even though callback throws
    notifier._delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
        None,
        _build_response("PERMISSION_REVOKED", "open_settings"),
        lambda: completion_called.append(True),
    )

    assert completion_called == [True]


# ---------------------------------------------------------------------------
# Tests — is_authorized
# ---------------------------------------------------------------------------


def test_is_authorized_true_for_status_2(install_fake_un) -> None:
    """status=2 (Authorized, Apple's enum) → is_authorized returns True."""
    install_fake_un(status=2)
    assert MenuBarNotifier().is_authorized() is True


def test_is_authorized_true_for_status_3(install_fake_un) -> None:
    """status=3 (Provisional) → is_authorized returns True."""
    install_fake_un(status=3)
    assert MenuBarNotifier().is_authorized() is True


def test_is_authorized_true_for_status_4(install_fake_un) -> None:
    """status=4 (Ephemeral, macOS 12+) → is_authorized returns True."""
    install_fake_un(status=4)
    assert MenuBarNotifier().is_authorized() is True


@pytest.mark.parametrize("status", [0, 1])
def test_is_authorized_false_for_other_statuses(install_fake_un, status: int) -> None:
    """0=NotDetermined / 1=Denied → unauthorized."""
    install_fake_un(status=status)
    assert MenuBarNotifier().is_authorized() is False


# ---------------------------------------------------------------------------
# Tests — PyObjC unavailable
# ---------------------------------------------------------------------------


def test_pyobjc_import_error_register_categories_silent(
    remove_un, caplog: pytest.LogCaptureFixture
) -> None:
    """Case 9a: register_categories() returns silently (warns only) on
    ImportError. No crash, no exception."""
    caplog.set_level(logging.WARNING)

    notifier = MenuBarNotifier()
    notifier.register_categories()  # must not raise


def test_pyobjc_import_error_post_returns_false(
    remove_un, caplog: pytest.LogCaptureFixture
) -> None:
    """Case 9b: post() returns False when PyObjC is unavailable."""
    caplog.set_level(logging.WARNING)

    notifier = MenuBarNotifier()
    result = notifier.post(
        category="FOUR_HOUR_PROMPT",
        title="t",
        body="b",
        identifier="x",
    )
    assert result is False


def test_pyobjc_import_error_is_authorized_false(remove_un) -> None:
    """is_authorized() returns False when PyObjC unavailable."""
    assert MenuBarNotifier().is_authorized() is False


# ---------------------------------------------------------------------------
# Tests — PII safety
# ---------------------------------------------------------------------------


def test_title_and_body_not_logged(
    install_fake_un, caplog: pytest.LogCaptureFixture
) -> None:
    """Case 10: notification title and body must never appear in log
    output, even at DEBUG."""
    install_fake_un(status=3)
    caplog.set_level(logging.DEBUG)

    secret_title = "MENU_BAR_SECRET_TITLE_TOKEN"
    secret_body = "MENU_BAR_SECRET_BODY_TOKEN_xyz"

    notifier = MenuBarNotifier()
    notifier.register_categories()
    notifier.post(
        category="FOUR_HOUR_PROMPT",
        title=secret_title,
        body=secret_body,
        identifier="pii-check",
    )

    captured = caplog.text
    assert secret_title not in captured, f"Title leaked into logs: {captured!r}"
    assert secret_body not in captured, f"Body leaked into logs: {captured!r}"


def test_user_info_not_logged(
    install_fake_un, caplog: pytest.LogCaptureFixture
) -> None:
    """user_info contents should not be logged either — they may carry
    identifiers a user wouldn't want in plaintext logs."""
    install_fake_un(status=3)
    caplog.set_level(logging.DEBUG)

    secret_token = "USER_INFO_SECRET_VALUE"

    notifier = MenuBarNotifier()
    notifier.register_categories()
    notifier.post(
        category="VOICE_NOTE_SAVE_FAILED",
        title="t",
        body="b",
        identifier="ui-check",
        user_info={"reason": secret_token},
    )

    assert secret_token not in caplog.text


# ---------------------------------------------------------------------------
# Tests — module surface / sanity
# ---------------------------------------------------------------------------


def test_module_exports() -> None:
    """Sanity: public API matches what the spec promises."""
    assert hasattr(notif_module, "MenuBarNotifier")
    notifier = notif_module.MenuBarNotifier()
    for method in ("register_categories", "post", "on_action", "is_authorized"):
        assert callable(getattr(notifier, method)), method


def test_category_definitions_match_spec() -> None:
    """The internal _category_definitions() table is the source of truth
    for categories. Lock it down here so spec drift is caught fast."""
    defs = dict(_category_definitions())
    assert defs == {
        "FOUR_HOUR_PROMPT": ["continue", "stop"],
        "PERMISSION_REVOKED": ["open_settings"],
        "MEETING_INTERRUPTED": ["open_settings"],
        "MISSED_MEETING": ["open_settings"],
        "MANUAL_SCREEN_ENDED": [],
        "CONTINUOUS_AUTO_STOPPED": [],
        "VOICE_NOTE_CAP_WARNING": ["stop", "continue"],
        "VOICE_NOTE_CAP_REACHED": ["open_save_window"],
        "VOICE_NOTE_SAVE_FAILED": ["retry"],
        "VOICE_NOTE_HOTKEY_FAILED": ["open_accessibility_settings"],
    }
