"""Shared test fixtures for Helios tests."""

import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio
import aiosqlite

from helios.clock import VirtualClock


@pytest_asyncio.fixture
async def tmp_db():
    """Create a temporary SQLite database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA foreign_keys = ON")
    yield db
    await db.close()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def migrations_dir():
    """Path to the migrations directory."""
    return Path(__file__).parent.parent / "migrations"


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Temporary directory for config files."""
    return tmp_path / "config"


@pytest.fixture
def virtual_clock():
    """Deterministic clock seeded at a fixed timestamp for tests."""
    return VirtualClock(initial_ts=1712345000.0)


# ---------------------------------------------------------------------------
# Audio fixture helper
# ---------------------------------------------------------------------------


def synth_wav(
    path: Path,
    duration_s: float,
    freq_hz: float = 440.0,
    sample_rate: int = 16000,
    n_channels: int = 1,
    sampwidth: int = 2,
    amplitude: float = 0.3,
    silent: bool = False,
) -> Path:
    """Write a mono int16 WAV (or arbitrary format for negative tests).

    Defaults produce the format Helios expects (16kHz mono int16). Override
    `sample_rate`, `n_channels`, or `sampwidth` to generate format-violating
    fixtures for the replay source's validation tests.

    `amplitude` is 0.0-1.0 of int16 max. `silent=True` writes zeros (used by
    no-audio chunker tests downstream).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(duration_s * sample_rate)
    if silent:
        samples = np.zeros(n_samples, dtype=np.int16)
    else:
        t = np.arange(n_samples) / sample_rate
        samples = (np.sin(2 * np.pi * freq_hz * t) * amplitude * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(sampwidth)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())
    return path
