"""Tests for config loading and generation."""

from pathlib import Path

from helios.config import HeliosConfig, load_config


def test_default_config_values():
    """Default config has expected values."""
    config = HeliosConfig()
    assert config.api.port == 3031
    assert config.api.bind_address == "127.0.0.1"
    assert config.audio.sample_rate == 16000
    assert config.audio.chunk_seconds == 30
    assert config.capture.calendar_pre_start_seconds == 60
    assert config.capture.calendar_post_end_seconds == 300
    assert config.storage.root == "~/.aegis/capture"
    assert config.transcription.model == "distil-large-v3"
    assert config.diarization.enabled is False
    assert config.voice_note.enabled is True
    assert config.voice_note.max_duration_seconds == 300


def test_config_generates_on_missing(tmp_path):
    """Config file is created with defaults when missing."""
    config_path = tmp_path / "capture.toml"
    config = load_config(path=config_path)
    assert config_path.exists()
    assert config.api.bearer_token != ""
    assert len(config.api.bearer_token) == 64  # 32 bytes hex


def test_config_permissions(tmp_path):
    """Generated config file has 600 permissions."""
    import stat
    config_path = tmp_path / "capture.toml"
    load_config(path=config_path)
    mode = config_path.stat().st_mode
    assert mode & stat.S_IRWXG == 0  # no group access
    assert mode & stat.S_IRWXO == 0  # no other access
