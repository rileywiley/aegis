#!/usr/bin/env python3
"""Aegis control script — start, stop, restart, and status.

Usage:
    python scripts/aegis_ctl.py start     # Start Aegis server
    python scripts/aegis_ctl.py stop      # Stop Aegis server
    python scripts/aegis_ctl.py status    # Show status
    python scripts/aegis_ctl.py restart   # Restart
"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path so we can import aegis.config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from rich.console import Console
from rich.table import Table

AEGIS_DIR = Path.home() / ".aegis"
LOG_DIR = AEGIS_DIR / "logs"
PID_FILE = AEGIS_DIR / "aegis.pid"
LOG_FILE = LOG_DIR / "aegis.log"

console = Console()


def _ensure_dirs() -> None:
    """Create ~/.aegis directories if they don't exist."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (AEGIS_DIR / "backups").mkdir(parents=True, exist_ok=True)


def _read_pid() -> int | None:
    """Read PID from file, return None if missing or invalid."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        return pid
    except (ValueError, OSError):
        return None


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_port_in_use(host: str, port: int) -> bool:
    """Check if a TCP port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def _get_settings():
    """Load Aegis settings (lazy to avoid import side-effects at module level)."""
    from aegis.config import get_settings
    return get_settings()


def cmd_start() -> None:
    """Start the Aegis server as a background process."""
    _ensure_dirs()
    settings = _get_settings()
    host = settings.aegis_host
    port = settings.aegis_port

    # Check if already running
    pid = _read_pid()
    if pid and _is_process_alive(pid):
        console.print(f"[yellow]Aegis is already running (PID {pid})[/yellow]")
        return

    # Check if port is in use by something else
    if _is_port_in_use(host, port):
        console.print(
            f"[red]Port {port} is already in use. "
            f"Another process may be bound to {host}:{port}.[/red]"
        )
        return

    # Clean up stale PID file
    if PID_FILE.exists():
        PID_FILE.unlink()

    # Launch uvicorn as a detached subprocess
    log_handle = open(LOG_FILE, "a")
    cmd = [
        sys.executable, "-m", "uvicorn",
        "aegis.main:app",
        "--host", host,
        "--port", str(port),
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_ROOT),
        start_new_session=True,
    )

    PID_FILE.write_text(str(proc.pid))
    console.print(f"[green]Aegis started (PID {proc.pid})[/green]")
    console.print(f"  Server:  http://{host}:{port}")
    console.print(f"  Log:     {LOG_FILE}")
    console.print(f"  PID:     {PID_FILE}")


def cmd_stop() -> None:
    """Stop the Aegis server."""
    pid = _read_pid()
    if pid is None:
        console.print("[yellow]No PID file found. Aegis may not be running.[/yellow]")
        return

    if not _is_process_alive(pid):
        console.print(f"[yellow]PID {pid} is not running. Cleaning up stale PID file.[/yellow]")
        PID_FILE.unlink(missing_ok=True)
        return

    console.print(f"Sending SIGTERM to PID {pid}...")
    os.kill(pid, signal.SIGTERM)

    # Wait up to 10 seconds for graceful shutdown
    for i in range(20):
        time.sleep(0.5)
        if not _is_process_alive(pid):
            console.print(f"[green]Aegis stopped (PID {pid})[/green]")
            PID_FILE.unlink(missing_ok=True)
            return

    # Force kill
    console.print(f"[yellow]Process did not stop gracefully. Sending SIGKILL...[/yellow]")
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    PID_FILE.unlink(missing_ok=True)
    console.print(f"[green]Aegis force-stopped (PID {pid})[/green]")


def cmd_status() -> None:
    """Show Aegis server status."""
    settings = _get_settings()
    host = settings.aegis_host
    port = settings.aegis_port

    table = Table(title="Aegis Status")
    table.add_column("Component", style="cyan")
    table.add_column("Status")
    table.add_column("Details", style="dim")

    # Process status
    pid = _read_pid()
    if pid and _is_process_alive(pid):
        table.add_row("Server process", "[green]Running[/green]", f"PID {pid}")
    elif pid:
        table.add_row("Server process", "[red]Dead[/red]", f"Stale PID {pid}")
    else:
        table.add_row("Server process", "[red]Not running[/red]", "No PID file")

    # HTTP health check
    port_open = _is_port_in_use(host, port)
    if port_open:
        table.add_row("HTTP endpoint", "[green]Reachable[/green]", f"http://{host}:{port}")
    else:
        table.add_row("HTTP endpoint", "[red]Unreachable[/red]", f"http://{host}:{port}")

    # Log file info
    if LOG_FILE.exists():
        size_kb = LOG_FILE.stat().st_size / 1024
        table.add_row("Log file", "[green]Exists[/green]", f"{size_kb:.1f} KB - {LOG_FILE}")
    else:
        table.add_row("Log file", "[dim]None[/dim]", str(LOG_FILE))

    # Database status
    db_url = settings.database_url
    # Parse host/port from the URL for a quick TCP check
    try:
        # postgresql+asyncpg://user@host:port/db
        from urllib.parse import urlparse
        parsed = urlparse(db_url.replace("+asyncpg", ""))
        db_host = parsed.hostname or "localhost"
        db_port = parsed.port or 5432
        if _is_port_in_use(db_host, db_port):
            table.add_row("Database", "[green]Reachable[/green]", f"{db_host}:{db_port}")
        else:
            table.add_row("Database", "[red]Unreachable[/red]", f"{db_host}:{db_port}")
    except Exception:
        table.add_row("Database", "[yellow]Unknown[/yellow]", db_url[:50])

    # Screenpipe status
    try:
        from urllib.parse import urlparse
        sp = urlparse(settings.screenpipe_url)
        sp_host = sp.hostname or "localhost"
        sp_port = sp.port or 3030
        if _is_port_in_use(sp_host, sp_port):
            table.add_row("Screenpipe", "[green]Reachable[/green]", f"{sp_host}:{sp_port}")
        else:
            table.add_row("Screenpipe", "[yellow]Not detected[/yellow]", f"{sp_host}:{sp_port}")
    except Exception:
        table.add_row("Screenpipe", "[yellow]Unknown[/yellow]", "")

    console.print(table)


def cmd_restart() -> None:
    """Restart the Aegis server."""
    console.print("[cyan]Restarting Aegis...[/cyan]")
    cmd_stop()
    time.sleep(1)
    cmd_start()


def cmd_backup() -> None:
    """Run database backup immediately."""
    backup_script = PROJECT_ROOT / "scripts" / "backup.py"
    if not backup_script.exists():
        console.print("[red]Backup script not found at scripts/backup.py[/red]")
        sys.exit(1)
    console.print("[bold]Running database backup...[/bold]")
    result = subprocess.run(
        [sys.executable, str(backup_script), "--rotate"],
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode == 0:
        console.print("[green]Backup complete.[/green]")
    else:
        console.print("[red]Backup failed.[/red]")
        sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2:
        console.print("[red]Usage: aegis {start|stop|status|restart|backup}[/red]")
        sys.exit(1)

    command = sys.argv[1].lower()
    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "restart": cmd_restart,
        "backup": cmd_backup,
    }

    if command not in commands:
        console.print(f"[red]Unknown command: {command}[/red]")
        console.print(f"Available commands: {', '.join(commands)}")
        sys.exit(1)

    commands[command]()


if __name__ == "__main__":
    main()
