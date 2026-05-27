"""Interactive CLI for stashing the Hugging Face access token.

Usage:
    python -m helios.scripts.set_hf_token

Prompts the user for the token, hides input, validates it's not empty,
writes to the macOS Keychain. Exits 0 on success, 1 on validation error
or keyring failure.
"""

from __future__ import annotations

import getpass
import sys

from helios.keychain import set_hf_token


def main() -> None:
    print("Helios — Set Hugging Face access token")
    print("Get a token at https://huggingface.co/settings/tokens")
    print("Then accept the model license at https://huggingface.co/pyannote/speaker-diarization-3.1")
    print()
    try:
        token = getpass.getpass("HF token (input hidden): ")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)
    if not token.strip():
        print("ERR: token is empty", file=sys.stderr)
        sys.exit(1)
    if set_hf_token(token):
        print("OK: token stashed in macOS Keychain (service=helios, account=huggingface)")
        sys.exit(0)
    print("ERR: keyring write failed — see daemon log", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":  # pragma: no cover - manual usage
    main()
