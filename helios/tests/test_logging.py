"""Tests for structured logging setup."""

from pathlib import Path

from helios.log import setup_logging, get_logger


def test_logging_creates_log_dir(tmp_path):
    """Log directory is created on setup."""
    log_dir = tmp_path / "logs"
    setup_logging(level="info", log_dir=log_dir)
    assert log_dir.exists()


def test_logger_returns_bound_logger():
    """get_logger returns a structlog logger."""
    log = get_logger("test")
    assert log is not None
