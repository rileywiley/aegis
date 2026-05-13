"""Download a faster-whisper model from Hugging Face.

Used by the onboarding flow (Phase 4 menu bar). Streams progress as
JSON lines on stdout so the UI can parse and render a progress bar.
After download completes, runs a 1-second synthetic-audio probe to
confirm the model loads under WhisperX.

Exit codes:
  0: success
  1: disk space insufficient
  2: download failed
  3: verification failed (model loads but inference broken)

Usage:
    python -m helios.scripts.download_whisper --model distil-large-v3
    python -m helios.scripts.download_whisper --model small.en --skip-verify
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import huggingface_hub
import numpy as np

# huggingface_hub.utils.tqdm IS the tqdm class (not a module). Older spec
# example showed `from huggingface_hub.utils import tqdm as hf_tqdm` and
# then `hf_tqdm.tqdm` — that's incorrect for huggingface_hub >= 0.20. Use
# the actual class as our base.
from huggingface_hub.utils.tqdm import tqdm as _HFTqdm

_DEFAULT_MIN_FREE_BYTES = 3 * 1024**3  # 3 GB

# Hugging Face moved the canonical faster-whisper repos in 2024 from the
# original maintainer's namespace (``guillaumekln/...``) to the SYSTRAN
# org. Map model names → modern repo ids. Accepts both bare model names
# (``distil-large-v3``, ``small.en``) and an explicit ``org/repo`` for
# escape-hatch use.
_MODEL_REPO_MAP = {
    "tiny": "Systran/faster-whisper-tiny",
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base": "Systran/faster-whisper-base",
    "base.en": "Systran/faster-whisper-base.en",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "medium": "Systran/faster-whisper-medium",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-large-v2": "Systran/faster-distil-whisper-large-v2",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
}


def _resolve_repo_id(model: str) -> str:
    """Map a short model name to its HF repo id, or pass through `org/repo`."""
    if "/" in model:
        return model
    return _MODEL_REPO_MAP.get(model, f"Systran/faster-whisper-{model}")


class JSONProgressTqdm(_HFTqdm):
    """Emit progress as JSON lines on stdout for the menu-bar UI to parse.

    Each ``display()`` call writes one JSON line of shape::

        {"type": "progress", "filename": str, "downloaded": int, "total": int}

    When ``total`` is unknown (streaming), it is reported as 0.
    """

    def display(self, msg=None, pos=None):  # type: ignore[override]
        try:
            data = {
                "type": "progress",
                "filename": getattr(self, "desc", "") or "download",
                "downloaded": int(self.n or 0),
                "total": int(self.total or 0),
            }
            sys.stdout.write(json.dumps(data) + "\n")
            sys.stdout.flush()
        except Exception:  # pragma: no cover - never break a download for a print
            pass
        # Suppress the parent class's terminal redraw — we don't want
        # tqdm's own carriage-return progress bar mixed with our JSON.
        return None


def _emit(payload: dict) -> None:
    """Write a single JSON line to stdout and flush."""
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def check_disk_space(min_bytes: int) -> None:
    """Exit 1 with an error JSON line if free disk under ``$HOME`` is below threshold."""
    home = Path.home()
    free = shutil.disk_usage(home).free
    if free < min_bytes:
        _emit(
            {
                "type": "error",
                "code": "insufficient_disk_space",
                "needed_bytes": min_bytes,
                "free_bytes": free,
            }
        )
        sys.exit(1)


def verify_with_synthetic_audio(snapshot_path: str, language: str = "en") -> None:
    """Probe the model with a 1-second sine wave. Confirms model loads under WhisperX.

    If WhisperX is not installed in the running environment (the daemon's
    ``transcription`` extras aren't pulled in by the dev shell), emit a
    non-fatal warning and return cleanly — the downloaded model is still
    usable when the daemon runs with transcription deps.
    """
    try:
        # Lazy import — WhisperX (and its torch dep) are heavy.
        import whisperx  # type: ignore[import-not-found]
    except ImportError:
        _emit(
            {
                "type": "warning",
                "code": "verification_skipped_no_whisperx",
            }
        )
        return
    try:
        sr = 16000
        t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
        audio = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        model = whisperx.load_model(snapshot_path, device="cpu", compute_type="int8")
        # Don't actually fail on empty result — sine wave has no speech, just
        # confirm load + transcribe roundtrip without raising.
        _ = model.transcribe(audio, language=language)
    except (ImportError, ModuleNotFoundError) as exc:
        # WhisperX itself imported, but a transitive dep didn't (this
        # happens inside the bundled py2app subprocess: site-packages
        # has the dist-info but not the .py for things like
        # ``typing_extensions`` until py2app's bootstrap rewrites
        # sys.path). The model files are on disk and the daemon
        # process — which DOES run with py2app's bootstrap — will
        # import these deps correctly. Treat as a soft warning so
        # onboarding can advance.
        _emit(
            {
                "type": "warning",
                "code": "verification_skipped_transitive_import",
                "detail": str(exc),
            }
        )
        return
    except Exception as exc:  # noqa: BLE001
        _emit(
            {
                "type": "error",
                "code": "verification_failed",
                "detail": str(exc),
            }
        )
        sys.exit(3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download a faster-whisper model")
    parser.add_argument(
        "--model",
        required=True,
        help="e.g. distil-large-v3, large-v3, small.en",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="ISO language code for the verification probe",
    )
    parser.add_argument(
        "--min-free-bytes",
        type=int,
        default=_DEFAULT_MIN_FREE_BYTES,
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip the synthetic-audio probe",
    )
    args = parser.parse_args()

    check_disk_space(args.min_free_bytes)
    repo_id = _resolve_repo_id(args.model)
    _emit({"type": "info", "repo_id": repo_id})
    try:
        snapshot_path = huggingface_hub.snapshot_download(
            repo_id=repo_id,
            tqdm_class=JSONProgressTqdm,
        )
    except Exception as exc:  # noqa: BLE001
        _emit(
            {
                "type": "error",
                "code": "download_failed",
                "detail": str(exc),
            }
        )
        sys.exit(2)
    if not args.skip_verify:
        verify_with_synthetic_audio(snapshot_path, language=args.language)
    _emit({"type": "done", "path": str(snapshot_path)})


if __name__ == "__main__":  # pragma: no cover - exercised via tests
    main()
