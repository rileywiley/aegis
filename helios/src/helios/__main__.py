"""Helios entry point — daemon or menu bar."""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Helios capture subsystem")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon (HTTP API server)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    args = parser.parse_args()

    if args.version:
        print("Helios 0.1.0")
        sys.exit(0)

    if args.daemon:
        from helios.daemon import run_daemon
        run_daemon(debug=args.debug)
    else:
        from helios.menubar.app import run_menubar
        run_menubar()


# Called both as `python -m helios` and via py2app's exec(__boot__.py)
main()
