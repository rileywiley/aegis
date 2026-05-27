"""Database connection pool — 1 writer + N readers."""

from pathlib import Path

import aiosqlite

from helios.log import get_logger

log = get_logger("db")

_PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA synchronous = NORMAL",
]


class DatabasePool:
    """SQLite connection pool: 1 writer + reader connections."""

    def __init__(self, path: Path):
        self.path = path
        self._writer: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Open the writer connection and apply pragmas."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = await aiosqlite.connect(self.path)
        for pragma in _PRAGMAS:
            await self._writer.execute(pragma)
        log.info("database_opened", path=str(self.path))

    async def close(self) -> None:
        """Close all connections."""
        if self._writer:
            await self._writer.close()
            self._writer = None
        log.info("database_closed")

    @property
    def writer(self) -> aiosqlite.Connection:
        """Get the writer connection."""
        if self._writer is None:
            raise RuntimeError("Database not opened")
        return self._writer

    async def reader(self) -> aiosqlite.Connection:
        """Get a reader connection (opens a new one each time for simplicity in Phase 0)."""
        conn = await aiosqlite.connect(self.path)
        for pragma in _PRAGMAS:
            await conn.execute(pragma)
        return conn
