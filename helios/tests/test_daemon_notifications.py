"""Tests for the daemon-side notifier (``helios.notifications.notify``).

These tests must run on any host, including non-macOS CI. The PyObjC
``UserNotifications`` framework is mocked by injecting a fake module into
``sys.modules`` before the notifier loads it.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any

import pytest

from helios.notifications import notify as notify_module
from helios.notifications.notify import DaemonNotifier, notify


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

    @classmethod
    def alloc(cls) -> "_FakeContent":
        return cls()

    def init(self) -> "_FakeContent":
        return self

    def setTitle_(self, title: str) -> None:
        self.title = title

    def setBody_(self, body: str) -> None:
        self.body = body


class _FakeRequest:
    """Stand-in for ``UNNotificationRequest``."""

    instances: list["_FakeRequest"] = []

    def __init__(self, identifier: str, content: _FakeContent, trigger: Any) -> None:
        self.identifier = identifier
        self.content = content
        self.trigger = trigger
        _FakeRequest.instances.append(self)

    @classmethod
    def requestWithIdentifier_content_trigger_(
        cls, identifier: str, content: _FakeContent, trigger: Any
    ) -> "_FakeRequest":
        return cls(identifier, content, trigger)


class _FakeCenter:
    """Stand-in for ``UNUserNotificationCenter``."""

    def __init__(self, status: int = 3, post_error: Any = None) -> None:
        self._status = status
        self._post_error = post_error
        self.posted_requests: list[_FakeRequest] = []

    def getNotificationSettingsWithCompletionHandler_(self, completion) -> None:
        # Real PyObjC fires completion on a background thread. Simulate that
        # so we exercise the ``call_soon_threadsafe`` path in the notifier.
        settings = _FakeSettings(self._status)

        def _run() -> None:
            completion(settings)

        threading.Thread(target=_run, daemon=True).start()

    def addNotificationRequest_withCompletionHandler_(self, request, completion) -> None:
        self.posted_requests.append(request)
        err = self._post_error

        def _run() -> None:
            completion(err)

        threading.Thread(target=_run, daemon=True).start()


class _FakeUserNotifications:
    """Module-shaped fake exposing the symbols ``notify.py`` imports."""

    def __init__(self, status: int = 3, post_error: Any = None) -> None:
        self._center = _FakeCenter(status=status, post_error=post_error)
        # Expose a class-with-a-classmethod so the notifier's
        # ``UNUserNotificationCenter.currentNotificationCenter()`` call works.
        center = self._center

        class _UNCenterShim:
            @staticmethod
            def currentNotificationCenter():
                return center

        self.UNUserNotificationCenter = _UNCenterShim
        self.UNMutableNotificationContent = _FakeContent
        self.UNNotificationRequest = _FakeRequest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def install_fake_un(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``UserNotifications`` module into ``sys.modules``.

    Returns a factory: ``install(status=3, post_error=None) -> _FakeUserNotifications``.
    The fake module is removed after the test by monkeypatch.
    """

    installed: list[_FakeUserNotifications] = []

    def install(status: int = 3, post_error: Any = None) -> _FakeUserNotifications:
        fake = _FakeUserNotifications(status=status, post_error=post_error)
        monkeypatch.setitem(sys.modules, "UserNotifications", fake)  # type: ignore[arg-type]
        # ``DaemonNotifier`` short-circuits when ``_in_app_bundle()``
        # returns False (it aborts with an uncatchable NSException
        # outside a real bundle). Flip the test seam so the fake
        # module's code path actually runs.
        monkeypatch.setattr(
            notify_module, "_FAKE_BUNDLE_FOR_TESTS", True
        )
        installed.append(fake)
        return fake

    yield install
    # monkeypatch tears down sys.modules entry automatically
    _FakeRequest.instances.clear()


@pytest.fixture
def remove_un(monkeypatch: pytest.MonkeyPatch):
    """Ensure ``UserNotifications`` import raises ImportError.

    Inserts ``None`` into ``sys.modules`` which makes Python raise
    ImportError on ``import UserNotifications``.
    """
    monkeypatch.setitem(sys.modules, "UserNotifications", None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_notify_authorized_returns_true_and_posts(install_fake_un) -> None:
    """Case 1: authorized=True → returns True, content posted."""
    fake = install_fake_un(status=3)

    notifier = DaemonNotifier()
    result = await notifier.post("Helios", "Capture started", identifier="test-1")

    assert result is True
    assert len(fake._center.posted_requests) == 1
    request = fake._center.posted_requests[0]
    assert request.identifier == "test-1"
    assert request.content.title == "Helios"
    assert request.content.body == "Capture started"


async def test_notify_unauthorized_returns_false_no_post(install_fake_un) -> None:
    """Case 2: authorized=False (Denied=1) → returns False, no post."""
    fake = install_fake_un(status=1)

    notifier = DaemonNotifier()
    result = await notifier.post("Helios", "Should not fire", identifier="test-2")

    assert result is False
    assert fake._center.posted_requests == []


async def test_is_authorized_true_for_status_2(install_fake_un) -> None:
    """authorizationStatus=2 (Authorized) → is_authorized returns True."""
    install_fake_un(status=2)
    assert await DaemonNotifier().is_authorized() is True


async def test_is_authorized_true_for_status_3(install_fake_un) -> None:
    """authorizationStatus=3 (Provisional) → is_authorized returns True."""
    install_fake_un(status=3)
    assert await DaemonNotifier().is_authorized() is True


async def test_is_authorized_true_for_status_4(install_fake_un) -> None:
    """authorizationStatus=4 (Ephemeral, macOS 12+) → is_authorized returns True."""
    install_fake_un(status=4)
    assert await DaemonNotifier().is_authorized() is True


@pytest.mark.parametrize("status", [0, 1])
async def test_is_authorized_false_for_other_statuses(
    install_fake_un, status: int
) -> None:
    """status 0/1 → is_authorized returns False.

    0=NotDetermined, 1=Denied.
    """
    install_fake_un(status=status)
    assert await DaemonNotifier().is_authorized() is False


async def test_pyobjc_import_error_returns_false(
    remove_un, caplog: pytest.LogCaptureFixture
) -> None:
    """Case 6: PyObjC ImportError → returns False, logs warning, no crash."""
    caplog.set_level(logging.WARNING)

    result = await DaemonNotifier().post("title", "body", identifier="test-6")

    assert result is False
    # ``is_authorized`` short-circuits on ImportError, so we don't crash.


async def test_identifier_passed_through(install_fake_un) -> None:
    """Case 7: caller-supplied identifier flows to UNNotificationRequest."""
    fake = install_fake_un(status=3)

    await DaemonNotifier().post("t", "b", identifier="custom-id-42")

    assert len(fake._center.posted_requests) == 1
    assert fake._center.posted_requests[0].identifier == "custom-id-42"


async def test_default_identifier_is_uuid(install_fake_un) -> None:
    """When no identifier is supplied, a UUID is generated."""
    fake = install_fake_un(status=3)

    await DaemonNotifier().post("t", "b")

    assert len(fake._center.posted_requests) == 1
    ident = fake._center.posted_requests[0].identifier
    # uuid4 stringified is 36 chars with 4 hyphens.
    assert isinstance(ident, str)
    assert len(ident) == 36
    assert ident.count("-") == 4


async def test_title_and_body_not_logged(
    install_fake_un, caplog: pytest.LogCaptureFixture
) -> None:
    """Case 8: title and body must not appear in log output (PII safety)."""
    install_fake_un(status=3)
    caplog.set_level(logging.DEBUG)

    secret_title = "PRIVATE_TITLE_TOKEN"
    secret_body = "PRIVATE_BODY_TOKEN_xyz"

    await DaemonNotifier().post(secret_title, secret_body, identifier="pii-check")

    # structlog's default factory writes JSON to a file in our setup, but
    # caplog also captures records via the std logging bridge. Either way,
    # we scan the captured text for the secrets.
    captured = caplog.text
    assert secret_title not in captured, (
        f"Title leaked into logs: {captured!r}"
    )
    assert secret_body not in captured, (
        f"Body leaked into logs: {captured!r}"
    )


async def test_module_level_notify_function(install_fake_un) -> None:
    """Case 9: module-level ``notify()`` mirrors ``DaemonNotifier().post()``."""
    fake = install_fake_un(status=3)

    result = await notify("Top-level", "via convenience", identifier="conv-1")

    assert result is True
    assert len(fake._center.posted_requests) == 1
    request = fake._center.posted_requests[0]
    assert request.identifier == "conv-1"
    assert request.content.title == "Top-level"
    assert request.content.body == "via convenience"


async def test_module_level_notify_unauthorized(install_fake_un) -> None:
    """Module-level convenience also respects authorization."""
    fake = install_fake_un(status=1)
    result = await notify("t", "b")
    assert result is False
    assert fake._center.posted_requests == []


def test_notify_module_exports() -> None:
    """Sanity: the public surface is what we documented."""
    assert hasattr(notify_module, "DaemonNotifier")
    assert hasattr(notify_module, "notify")
    assert callable(notify_module.notify)
