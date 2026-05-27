#!/bin/bash
# Build Helios.app via py2app
# Uses Python 3.13 (py2app incompatible with 3.14 — no distutils)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HELIOS_DIR="$(dirname "$SCRIPT_DIR")/helios"
BUILD_VENV="$HELIOS_DIR/.build-venv"

cd "$HELIOS_DIR"

# Create a build venv with Python 3.13
echo "Setting up build environment with Python 3.13..."
rm -rf "$BUILD_VENV"
uv venv "$BUILD_VENV" --python 3.13
export VIRTUAL_ENV="$BUILD_VENV"
export PATH="$BUILD_VENV/bin:$PATH"
unset CONDA_PREFIX

# Phase 3: install the [transcription] extras so the bundled venv has
# whisperx + pyannote.audio + torch + torchaudio in site-packages. py2app's
# modulegraph picks them up via the `packages` list in setup.py.
uv pip install -e '.[transcription]' -e ../shared py2app "setuptools<72"

rm -rf build dist

# py2app 0.28 rejects install_requires from pyproject.toml.
# Temporarily hide pyproject.toml so setuptools only sees setup.py.
mv pyproject.toml pyproject.toml.bak
trap "mv pyproject.toml.bak pyproject.toml" EXIT

python setup.py py2app

# transformers / huggingface_hub validate dependency versions at import
# time using `importlib.metadata`, which needs the `.dist-info/` metadata
# directories alongside each bundled package. py2app ships only the
# package code, not the metadata. Copy every dist-info from the build
# venv into the bundle so transformers' dep check passes.
echo "Copying .dist-info metadata into bundle..."
BUILD_SP="$BUILD_VENV/lib/python3.13/site-packages"
BUNDLE_SP="dist/Helios.app/Contents/Resources/lib/python3.13"
if [ -d "$BUILD_SP" ] && [ -d "$BUNDLE_SP" ]; then
    find "$BUILD_SP" -maxdepth 1 -name "*.dist-info" -type d | while read -r di; do
        cp -R "$di" "$BUNDLE_SP/" 2>/dev/null || true
    done
fi

# torchaudio's libtorchaudio.so + _torchaudio.so reference @rpath for
# libtorch.dylib / libtorch_cpu.dylib / libc10.dylib, but they don't
# carry an LC_RPATH entry. In a normal pip install, torch's __init__
# adds torch/lib to dyld's search path before torchaudio loads; in our
# py2app bundle that wiring breaks. Patch each torchaudio binary with an
# LC_RPATH pointing at @loader_path/../../torch/lib so the dynamic
# linker can find the bundled torch dylibs.
# pyannote.audio is a PEP 420 namespace package — `pyannote/` has no
# top-level __init__.py, so py2app's modulegraph can't enumerate it.
# We exclude it from setup.py's modulegraph crawl and copy the tree by
# hand from the build venv. Subpackages (pyannote/audio, pyannote/core)
# all have proper __init__.py and resolve normally at runtime.
echo "Copying pyannote namespace package + transitive deps from build venv..."
PYANNOTE_SRC="$BUILD_VENV/lib/python3.13/site-packages/pyannote"
PYANNOTE_DST="dist/Helios.app/Contents/Resources/lib/python3.13/pyannote"
if [ -d "$PYANNOTE_SRC" ]; then
    cp -R "$PYANNOTE_SRC" "$PYANNOTE_DST"
    # pyannote.audio depends on a handful of single-package wheels py2app's
    # modulegraph misses. Copy them too if they're in the build venv but
    # not already in the bundle.
    for pkg in asteroid_filterbanks einops omegaconf hydra hydra_colorlog \
               lightning_fabric pytorch_metric_learning pyannoteai_sdk \
               speechbrain semver primePy sortedcontainers \
               torch_audiomentations julius torch_pitch_shift; do
        SRC="$BUILD_VENV/lib/python3.13/site-packages/$pkg"
        DST="dist/Helios.app/Contents/Resources/lib/python3.13/$pkg"
        if [ -d "$SRC" ] && [ ! -d "$DST" ]; then
            cp -R "$SRC" "$DST"
        fi
    done
    # pyannote.audio's telemetry module phones home via opentelemetry and
    # uses entry-point lookups that don't survive py2app bundling
    # (importlib_metadata can't resolve `opentelemetry.context.contextvars_context`).
    # Stub out telemetry/metrics.py with no-op functions — pyannote import
    # chain reaches it via `pyannote.audio.core.model` which only uses the
    # `track_*` decorators. Eliminates the opentelemetry dependency
    # entirely while leaving pyannote functional. Run-from-source users
    # keep full telemetry; only the bundle gets the stub.
    METRICS_FILE="$PYANNOTE_DST/audio/telemetry/metrics.py"
    if [ -f "$METRICS_FILE" ]; then
        cat > "$METRICS_FILE" <<'PYEOF'
"""Stubbed pyannote telemetry — no-op replacement for the bundled .app.
The upstream module imports opentelemetry which uses entry-point lookups
that fail under py2app. Telemetry is non-essential, so we replace it
with no-ops so pyannote's core model import succeeds.
"""

from __future__ import annotations
from typing import Any


def set_telemetry_metrics(*_args: Any, **_kwargs: Any) -> None:
    pass


def set_opentelemetry_log_level(*_args: Any, **_kwargs: Any) -> None:
    pass


def track_model_init(*_args: Any, **_kwargs: Any) -> None:
    pass


def track_pipeline_init(*_args: Any, **_kwargs: Any) -> None:
    pass


def track_pipeline_apply(*_args: Any, **_kwargs: Any) -> None:
    pass
PYEOF
    fi
fi

# py2app's bundled liblzma.5.dylib has been observed to fail strict
# codesign validation ("internal error in Code Signing subsystem"). When
# the broken signature lingers, Python's `_lzma.so` refuses to load it
# (errno=85), which breaks any import that transitively pulls in the
# `lzma` stdlib module — including `torchvision.datasets`. Replace the
# bundled copy with a fresh one straight from Homebrew, fix the install
# id to the bundle-relative path, and let the later codesign pass sign
# it cleanly.
echo "Replacing liblzma.5.dylib with fresh Homebrew copy..."
LZ_BUNDLED="dist/Helios.app/Contents/Frameworks/liblzma.5.dylib"
LZ_BREW=$(brew --prefix xz 2>/dev/null)/lib/liblzma.5.dylib
if [ -f "$LZ_BUNDLED" ] && [ -f "$LZ_BREW" ]; then
    cp -f "$LZ_BREW" "$LZ_BUNDLED"
    chmod u+w "$LZ_BUNDLED"
    install_name_tool -id "@executable_path/../Frameworks/liblzma.5.dylib" "$LZ_BUNDLED" 2>/dev/null || true
fi

echo "Patching torchaudio + torchvision rpaths to find bundled torch libs..."
SP="dist/Helios.app/Contents/Resources/lib/python3.13"

# torchaudio: .so files live at torchaudio/lib/, torch dylibs at torch/lib/.
TA_LIB="$SP/torchaudio/lib"
if [ -d "$TA_LIB" ]; then
    for so in "$TA_LIB"/libtorchaudio.so "$TA_LIB"/_torchaudio.so \
              "$TA_LIB"/libtorchaudio_sox.so "$TA_LIB"/_torchaudio_sox.so; do
        if [ -f "$so" ]; then
            # torch dylibs (libtorch.dylib, libc10.dylib) at torch/lib —
            # two ups + one down from torchaudio/lib.
            install_name_tool -add_rpath "@loader_path/../../torch/lib" "$so" 2>/dev/null || true
            # torchaudio's own siblings (libtorchaudio.so etc.) live in the
            # same dir as the loader.
            install_name_tool -add_rpath "@loader_path" "$so" 2>/dev/null || true
        fi
    done
fi

# torchvision: _C.so + image.so live at torchvision/, torch dylibs at torch/lib/.
# _C.so registers torchvision::nms at import time — without these rpaths the
# operator never registers and transformers' image utils fail at import.
TV_DIR="$SP/torchvision"
if [ -d "$TV_DIR" ]; then
    for so in "$TV_DIR"/_C.so "$TV_DIR"/image.so; do
        if [ -f "$so" ]; then
            # One up to site-packages, then into torch/lib.
            install_name_tool -add_rpath "@loader_path/../torch/lib" "$so" 2>/dev/null || true
        fi
    done
fi

# Sign every bundled dylib first — `codesign --deep` skips dylibs that
# are "not signed at all" (a Homebrew-built liblzma / libssl, etc).
# Walk Contents/Frameworks/ and Resources/ and sign each .dylib before
# resigning the .app. Must run AFTER any install_name_tool patches —
# install_name_tool invalidates existing signatures.
echo "Pre-signing bundled dylibs..."
find dist/Helios.app -name "*.dylib" -type f -exec codesign --force --sign - {} \; 2>/dev/null || true
find dist/Helios.app -name "*.so" -type f -exec codesign --force --sign - {} \; 2>/dev/null || true

# Ad-hoc sign the bundle. `--deep` walks every subcomponent. Some
# Homebrew-built dylibs (notably liblzma.5.dylib) fail codesign's
# strict-validation post-step with "code object is not signed at all"
# even after we pre-signed them above; this is cosmetic — the .app
# launches and runs. We capture the exit code for diagnostics but do
# not abort the build on it.
set +e
codesign --force --deep --sign - dist/Helios.app
sign_rc=$?
set -e
if [ $sign_rc -ne 0 ]; then
    echo "WARNING: codesign exited $sign_rc — some subcomponents may have"
    echo "         strict-validation issues. Verifying the bundle launches:"
    if dist/Helios.app/Contents/MacOS/Helios --version >/dev/null 2>&1; then
        echo "         OK: bundle's --version succeeds; continuing."
    else
        echo "         FATAL: bundle does not launch. Aborting."
        exit 1
    fi
fi
echo "Helios.app built and ad-hoc signed at helios/dist/Helios.app"

# Cleanup build venv
rm -rf "$BUILD_VENV"
