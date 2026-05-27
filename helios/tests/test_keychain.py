"""Tests for the macOS Keychain wrapper used to store the HF token.

The real macOS Keychain is never touched. ``keyring.{get,set,delete}_password``
is monkeypatched onto a ``_FakeKeyring`` instance for every test.
"""

from __future__ import annotations

import keyring
import keyring.errors
import pytest

from helios.keychain import (
    _ACCOUNT_HF,
    _SERVICE,
    clear_hf_token,
    get_hf_token,
    set_hf_token,
)


class _FakeKeyring:
    """In-memory stand-in for the keyring backend used in tests."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        # Set to "read", "write", or "delete" to make the corresponding
        # method raise KeyringError. Used to exercise error paths.
        self.fail_mode: str | None = None

    def get_password(self, service: str, account: str) -> str | None:
        if self.fail_mode == "read":
            raise keyring.errors.KeyringError("read fail")
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, token: str) -> None:
        if self.fail_mode == "write":
            raise keyring.errors.KeyringError("write fail")
        self.store[(service, account)] = token

    def delete_password(self, service: str, account: str) -> None:
        if self.fail_mode == "delete":
            raise keyring.errors.KeyringError("delete fail")
        try:
            del self.store[(service, account)]
        except KeyError as exc:
            raise keyring.errors.PasswordDeleteError(str(exc))


@pytest.fixture
def fake_kr(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    return fake


# ---------------------------------------------------------------------------
# get_hf_token
# ---------------------------------------------------------------------------


def test_get_hf_token_returns_none_when_nothing_stored(fake_kr: _FakeKeyring) -> None:
    assert get_hf_token() is None


def test_get_hf_token_returns_value_when_set(fake_kr: _FakeKeyring) -> None:
    fake_kr.store[(_SERVICE, _ACCOUNT_HF)] = "hf_abc123"
    assert get_hf_token() == "hf_abc123"


def test_get_hf_token_returns_none_on_keyring_error(fake_kr: _FakeKeyring) -> None:
    fake_kr.fail_mode = "read"
    # Must not raise — diarization worker depends on graceful None return.
    assert get_hf_token() is None


def test_get_hf_token_treats_empty_string_as_missing(fake_kr: _FakeKeyring) -> None:
    """Some keyring backends return ``""`` rather than ``None`` for missing keys."""
    fake_kr.store[(_SERVICE, _ACCOUNT_HF)] = ""
    assert get_hf_token() is None


# ---------------------------------------------------------------------------
# set_hf_token
# ---------------------------------------------------------------------------


def test_set_hf_token_round_trips(fake_kr: _FakeKeyring) -> None:
    assert set_hf_token("hf_xyz789") is True
    assert get_hf_token() == "hf_xyz789"
    # And stashed under the documented service/account.
    assert fake_kr.store[(_SERVICE, _ACCOUNT_HF)] == "hf_xyz789"


def test_set_hf_token_rejects_empty_string(fake_kr: _FakeKeyring) -> None:
    with pytest.raises(ValueError):
        set_hf_token("")


def test_set_hf_token_rejects_whitespace_only(fake_kr: _FakeKeyring) -> None:
    with pytest.raises(ValueError):
        set_hf_token("   ")


def test_set_hf_token_returns_false_on_write_failure(fake_kr: _FakeKeyring) -> None:
    fake_kr.fail_mode = "write"
    assert set_hf_token("hf_will_fail") is False


def test_set_hf_token_strips_surrounding_whitespace(fake_kr: _FakeKeyring) -> None:
    assert set_hf_token("  hf_padded  \n") is True
    assert fake_kr.store[(_SERVICE, _ACCOUNT_HF)] == "hf_padded"


# ---------------------------------------------------------------------------
# clear_hf_token
# ---------------------------------------------------------------------------


def test_clear_hf_token_removes_existing(fake_kr: _FakeKeyring) -> None:
    fake_kr.store[(_SERVICE, _ACCOUNT_HF)] = "hf_to_remove"
    assert clear_hf_token() is True
    assert get_hf_token() is None


def test_clear_hf_token_returns_true_when_nothing_stored(fake_kr: _FakeKeyring) -> None:
    # Backend raises PasswordDeleteError; we treat that as success.
    assert clear_hf_token() is True


def test_clear_hf_token_returns_false_on_keyring_error(fake_kr: _FakeKeyring) -> None:
    fake_kr.store[(_SERVICE, _ACCOUNT_HF)] = "hf_present"
    fake_kr.fail_mode = "delete"
    assert clear_hf_token() is False
    # Token still present because the delete failed.
    assert fake_kr.store[(_SERVICE, _ACCOUNT_HF)] == "hf_present"
