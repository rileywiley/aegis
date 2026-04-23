#!/usr/bin/env python3
"""Aegis database backup with rotation.

Usage:
    python scripts/backup.py              # Run backup now
    python scripts/backup.py --rotate     # Run backup and clean old ones (30-day retention)
"""

import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from rich.console import Console

AEGIS_DIR = Path.home() / ".aegis"
BACKUP_DIR = AEGIS_DIR / "backups"
RETENTION_DAYS = 30

console = Console()


def _ensure_dirs() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def _parse_db_url(url: str) -> dict:
    """Extract host, port, user, dbname from DATABASE_URL."""
    # Strip the +asyncpg driver suffix so urlparse handles it
    clean = url.replace("+asyncpg", "")
    parsed = urlparse(clean)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "user": parsed.username or "postgres",
        "dbname": parsed.path.lstrip("/") or "aegis",
    }


def _find_pg_dump() -> str | None:
    """Find pg_dump binary."""
    path = shutil.which("pg_dump")
    if path:
        return path
    # Common Homebrew / Postgres.app locations
    for candidate in [
        "/opt/homebrew/bin/pg_dump",
        "/usr/local/bin/pg_dump",
        "/Applications/Postgres.app/Contents/Versions/latest/bin/pg_dump",
    ]:
        if Path(candidate).exists():
            return candidate
    return None


def run_backup() -> Path | None:
    """Run pg_dump and return the path to the backup file, or None on failure."""
    _ensure_dirs()

    from aegis.config import get_settings
    settings = get_settings()

    pg_dump = _find_pg_dump()
    if not pg_dump:
        console.print("[red]pg_dump not found. Install PostgreSQL client tools.[/red]")
        return None

    db = _parse_db_url(settings.database_url)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dump_file = BACKUP_DIR / f"aegis_{timestamp}.dump"

    cmd = [
        pg_dump,
        "-h", db["host"],
        "-p", str(db["port"]),
        "-U", db["user"],
        "-Fc",  # custom format (compressed, supports pg_restore)
        "-f", str(dump_file),
        db["dbname"],
    ]

    console.print(f"[cyan]Running backup: {db['dbname']} @ {db['host']}:{db['port']}[/cyan]")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        console.print(f"[red]Backup failed:[/red]\n{result.stderr}")
        dump_file.unlink(missing_ok=True)
        return None

    # Validate file
    if not dump_file.exists() or dump_file.stat().st_size == 0:
        console.print("[red]Backup file is empty or missing.[/red]")
        dump_file.unlink(missing_ok=True)
        return None

    size_mb = dump_file.stat().st_size / (1024 * 1024)
    console.print(f"[green]Backup complete:[/green] {dump_file}")
    console.print(f"  Size: {size_mb:.2f} MB")

    return dump_file


def rotate_backups() -> None:
    """Delete backup files older than RETENTION_DAYS."""
    if not BACKUP_DIR.exists():
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    removed = 0

    for f in sorted(BACKUP_DIR.glob("aegis_*.dump")):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            f.unlink()
            console.print(f"  [dim]Removed old backup: {f.name}[/dim]")
            removed += 1

    remaining = list(BACKUP_DIR.glob("aegis_*.dump"))
    console.print(
        f"[cyan]Rotation complete:[/cyan] "
        f"removed {removed}, {len(remaining)} backups remaining"
    )


def main() -> None:
    do_rotate = "--rotate" in sys.argv

    dump_path = run_backup()

    if dump_path is None:
        sys.exit(1)

    if do_rotate:
        console.print()
        rotate_backups()

    # Summary
    remaining = list(BACKUP_DIR.glob("aegis_*.dump"))
    total_mb = sum(f.stat().st_size for f in remaining) / (1024 * 1024)
    console.print()
    console.print(f"[cyan]Backup directory:[/cyan] {BACKUP_DIR}")
    console.print(f"[cyan]Total backups:[/cyan] {len(remaining)} ({total_mb:.2f} MB)")


if __name__ == "__main__":
    main()
