"""Migration runner — applies SQL migrations from helios/migrations/."""

import os
import time
from pathlib import Path

import aiosqlite

from helios.log import get_logger

log = get_logger("migrations")


def get_migrations_dir() -> Path:
    """Resolve the migrations directory in dev or py2app bundle."""
    # py2app sets RESOURCEPATH to Contents/Resources/
    resource_path = os.environ.get("RESOURCEPATH")
    if resource_path:
        return Path(resource_path) / "migrations"
    # Dev: this file lives at helios/src/helios/db/migrations.py;
    # migrations live at helios/migrations/
    return Path(__file__).resolve().parents[3] / "migrations"


async def run_migrations(db: aiosqlite.Connection, migrations_dir: Path) -> int:
    """Apply all pending migrations. Returns the new schema version."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    await db.commit()

    current = await _get_current_version(db)
    files = sorted(migrations_dir.glob("*.sql"))

    for path in files:
        version = int(path.stem.split("_")[0])
        if version <= current:
            continue
        sql = path.read_text()
        await db.executescript(sql)
        await db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, time.time()),
        )
        await db.commit()
        log.info("migration_applied", version=version, file=path.name)
        current = version

    return current


async def _get_current_version(db: aiosqlite.Connection) -> int:
    """Get the current schema version, or 0 if no migrations applied."""
    try:
        cursor = await db.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        return row[0] or 0 if row else 0
    except Exception:
        return 0
