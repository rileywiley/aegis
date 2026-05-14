"""py2app configuration for Helios.app bundle."""

import sys

from setuptools import setup

# py2app's modulegraph uses recursive AST walks. PyTorch's deeply-nested
# conditional imports (torch.distributed / torch.fx / torch.nn) blow past
# the default 1000-frame stack on Python 3.13. Bump well above the deepest
# torch sub-tree before py2app starts crawling.
sys.setrecursionlimit(10000)

APP = ["src/helios/__main__.py"]
DATA_FILES = [
    ("icons", [
        "icons/helios_not_running_template.png",
        "icons/helios_not_running_template@2x.png",
        "icons/helios_armed_template.png",
        "icons/helios_armed_template@2x.png",
        "icons/helios_recording_template.png",
        "icons/helios_recording_template@2x.png",
        "icons/helios_paused_template.png",
        "icons/helios_paused_template@2x.png",
        "icons/helios_error_template.png",
        "icons/helios_error_template@2x.png",
    ]),
    ("bin", ["bin/ScreenCaptureHelper"]),
    ("migrations", ["migrations/001_initial.sql"]),
]

OPTIONS = {
    "argv_emulation": False,
    "packages": [
        "rumps", "numpy", "aiosqlite",
        "uvicorn", "fastapi", "pydantic", "structlog",
        "httpx", "keyring", "watchfiles",
        # Must ship as a real on-disk package (not zipped) — sounddevice's
        # CFFI lookup does dlopen() against _sounddevice_data.__path__, and
        # native dylibs cannot be loaded from inside python313.zip.
        "_sounddevice_data",
        # Phase 3 [transcription] stack. These pull in heavy native deps
        # (PyTorch, ctranslate2, libomp) and lots of dynamic-import sub-
        # modules; listing them as packages tells py2app to ship the whole
        # tree on disk rather than zipping. Bundle grows ~2 GB but the
        # daemon's lazy `import whisperx` / `import pyannote.audio` calls
        # actually resolve at runtime.
        "torch",
        "torchaudio",
        "whisperx",
        "faster_whisper",
        "ctranslate2",
        "transformers",
        "huggingface_hub",
        "soundfile",  # whisperx audio loading (the .py wrapper)
        # libsndfile native dylib — shipped on disk for the same reason
        # as _sounddevice_data/portaudio (CFFI dlopen against __path__).
        "_soundfile_data",
        "scipy",
        "sklearn",
        "joblib",
        # Common transitive deps py2app's modulegraph misses for some
        # combinations of imports in the ML stack.
        "tqdm",
        "requests",
        "urllib3",
        "charset_normalizer",
        "certifi",
        "idna",
        # huggingface_hub + transformers transitive deps.
        "filelock",
        "safetensors",
        "tokenizers",
        "regex",
        "yaml",          # PyYAML imports as `yaml`
        "packaging",
        # pyannote ships as a PEP 420 namespace package — `pyannote/` has
        # no top-level __init__.py. py2app's `packages` collector uses
        # imp.find_module which can't discover dotted namespace children,
        # so they go in `includes` below (which handles dotted names).
    ],
    # sounddevice + _sounddevice are single-file modules, not packages.
    # Listing them under `includes` lets py2app bundle them into the zip
    # while _sounddevice_data ships outside the zip with its bundled dylib.
    #
    # anyio._backends._asyncio is loaded dynamically by anyio (via
    # importlib + sniffio), so py2app's modulegraph misses it. httpx
    # depends on anyio for async I/O — without this include, the daemon's
    # calendar poll fails with ModuleNotFoundError at first request.
    "includes": [
        "helios",
        "sounddevice",
        "_sounddevice",
        "anyio._backends",
        "anyio._backends._asyncio",
        # PyObjC's UserNotifications framework. Drives action-button
        # banners (FOUR_HOUR_PROMPT, VOICE_NOTE_CAP_WARNING, ...).
        # py2app's modulegraph doesn't discover this on its own — the
        # menubar imports it lazily and silently downgrades to "no
        # notifications" on ImportError. Listing it as an include
        # forces py2app to ship the framework wrapper.
        "UserNotifications",
        # Common dynamic-import landmines for the transcription stack:
        # transformers loads model classes via importlib at runtime;
        # ctranslate2 uses dlopen + a cffi-style native shim. py2app's
        # modulegraph misses these.
        "ctranslate2._ext",
        "torch._C",
        "torch.classes",
        "torchaudio._extension",
        # Single-file modules pulled in by torch / soundfile / sklearn.
        "typing_extensions",
        "_soundfile",
        "threadpoolctl",
    ],
    # py2app's `imp.find_module` doesn't understand PEP 420 namespace
    # packages, and the `pyannote` directory has no top-level __init__.py.
    # We exclude it from py2app's modulegraph crawl (this list) so it
    # doesn't blow up at build time, and the build script copies the
    # `pyannote/` tree from the build venv by hand into the bundle as a
    # post-build step. Subpackages (pyannote/audio, pyannote/core) have
    # their own __init__.py and resolve fine at runtime once on disk.
    "excludes": [
        "pyannote",
        "pyannote.audio",
        "pyannote.core",
        "pyannote.database",
        "pyannote.metrics",
        "pyannote.pipeline",
    ],
    "plist": {
        "CFBundleName": "Helios",
        "CFBundleDisplayName": "Helios",
        "CFBundleIdentifier": "com.aegis.helios",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,  # menu bar app, no dock icon
        "NSMicrophoneUsageDescription": "Helios captures meeting audio for transcription.",
        "NSAppleEventsUsageDescription": "Helios sends notifications for meeting events.",
    },
}

setup(
    name="Helios",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    install_requires=[],  # py2app 0.28 errors if install_requires is populated
)
