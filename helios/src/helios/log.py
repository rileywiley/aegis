"""Structured JSON logging for Helios using structlog."""

import logging
import sys
from pathlib import Path

import structlog


_SENSITIVE_KEYS = frozenset({"bearer_token", "transcript_text", "text", "hf_token", "password", "secret"})


def _filter_sensitive(_, __, event_dict: dict) -> dict:
    """Redact sensitive fields from log output."""
    for key in list(event_dict.keys()):
        if key in _SENSITIVE_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def setup_logging(level: str = "info", log_dir: Path | None = None) -> None:
    """Configure structlog with JSON output to file and console."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Ensure log directory exists
    if log_dir is None:
        log_dir = Path("~/.aegis/capture/logs").expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    # File handler with rotation
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        log_dir / "helios.log",
        maxBytes=50 * 1024 * 1024,
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)

    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(log_level)

    logging.basicConfig(
        level=log_level,
        handlers=[file_handler, console_handler],
        format="%(message)s",
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", key="ts"),
            _filter_sensitive,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=open(log_dir / "helios.log", "a")),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """Get a structlog logger."""
    return structlog.get_logger(name)
