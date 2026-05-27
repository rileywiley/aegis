"""Keychain access for the Helios daemon.

Stores the Hugging Face access token used by pyannote diarization
(Track 3C). Token is held in the macOS Keychain under service
``"helios"``, account ``"huggingface"``. The daemon reads it lazily
when the diarization worker first attempts to load its model.

Functions return ``None`` rather than raising on missing token /
keyring backend errors so daemon startup never fails because of an
absent token — the diarization component simply reports
``unavailable`` with reason ``token_missing``.
"""

from __future__ import annotations

import keyring
import keyring.errors

from helios.log import get_logger

_log = get_logger("keychain")

_SERVICE = "helios"
_ACCOUNT_HF = "huggingface"


def get_hf_token() -> str | None:
    """Return the stored HF token, or ``None`` if missing / keyring inaccessible."""
    try:
        token = keyring.get_password(_SERVICE, _ACCOUNT_HF)
        return token if token else None
    except keyring.errors.KeyringError as exc:
        _log.warning("keychain_read_failed", error=str(exc))
        return None


def set_hf_token(token: str) -> bool:
    """Stash a HF token. Returns True on success, False on keyring error.

    Empty / whitespace-only tokens are rejected; callers should call
    :func:`clear_hf_token` to remove an existing token instead.
    """
    if not token or not token.strip():
        raise ValueError("token must be non-empty")
    try:
        keyring.set_password(_SERVICE, _ACCOUNT_HF, token.strip())
        return True
    except keyring.errors.KeyringError as exc:
        _log.error("keychain_write_failed", error=str(exc))
        return False


def clear_hf_token() -> bool:
    """Delete the stored HF token. Returns True on success or if no token was stored."""
    try:
        keyring.delete_password(_SERVICE, _ACCOUNT_HF)
        return True
    except keyring.errors.PasswordDeleteError:
        # No token stored — that's the desired state, treat as success.
        return True
    except keyring.errors.KeyringError as exc:
        _log.error("keychain_delete_failed", error=str(exc))
        return False
