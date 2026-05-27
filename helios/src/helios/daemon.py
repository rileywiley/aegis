"""Helios daemon — runs the HTTP API server."""

import os

import uvicorn

from helios.config import load_config
from helios.log import setup_logging, get_logger


def run_daemon(debug: bool = False) -> None:
    """Start the Helios daemon."""
    config = load_config()

    level = "debug" if debug else config.logging.level
    setup_logging(level=level)
    log = get_logger("daemon")

    log.info("daemon_starting", port=config.api.port, debug=debug)
    log.info(
        "source_factory_mode",
        replay=os.environ.get("HELIOS_REPLAY") == "1",
    )

    from helios.api import create_app
    app = create_app()

    uvicorn.run(
        app,
        host=config.api.bind_address,
        port=config.api.port,
        log_level=level,
    )
