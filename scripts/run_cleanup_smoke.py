"""§12.6 smoke driver — run cleanup once with raw_audio_days=0.

Connects to the live ``~/.aegis/capture/index.db``, instantiates a
``CleanupWorker`` with an overridden retention config (0 days), and
calls ``run_cleanup`` synchronously. Verifies trash semantics: all
transcribed WAVs with non-null paths get moved to ``trash/``, the
``path`` column becomes NULL, and untranscribed chunks are skipped.

This is a one-shot tool — the daemon's own cleanup worker stays
running on its daily schedule. The override here is in-memory only.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import aiosqlite


def _bootstrap_helios_import() -> None:
    """Resolve the bundled `helios` package from /Applications/Helios.app."""
    candidates = [
        Path("/Applications/Helios.app/Contents/Resources/lib/python3.13"),
        Path(__file__).parent.parent / "helios" / "src",
    ]
    for c in candidates:
        if (c / "helios").exists():
            sys.path.insert(0, str(c))
            return
    raise SystemExit("could not locate helios package")


_bootstrap_helios_import()

# Late imports so sys.path is set first.
from helios.clock import RealClock  # noqa: E402
from helios.config import HeliosConfig, load_config  # noqa: E402
from helios.workers.cleanup import CleanupWorker  # noqa: E402


async def _main() -> int:
    capture_root = Path.home() / ".aegis" / "capture"
    db_path = capture_root / "index.db"
    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist", file=sys.stderr)
        return 1

    toml_path = capture_root / "capture.toml"
    cfg: HeliosConfig = load_config(toml_path)
    cfg.retention.raw_audio_days = 0
    print(
        f"cleanup smoke: db={db_path} raw_audio_days={cfg.retention.raw_audio_days}"
        f" cleanup_hour_local={cfg.retention.cleanup_hour_local}"
    )

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    try:
        worker = CleanupWorker(db=db, config=cfg, clock=RealClock())
        report = await worker.run_cleanup()
        print(
            f"cleanup_report: archived={report.archived}"
            f" skipped_untranscribed={report.skipped_untranscribed}"
            f" purged={report.purged} errors={report.errors}"
        )

        trash_dir = capture_root / "trash"
        trash_files = list(trash_dir.glob("*")) if trash_dir.exists() else []
        print(f"trash_dir entries: {len(trash_files)}")

        async with db.execute(
            "SELECT COUNT(*) FROM audio_chunks WHERE status='transcribed' AND path IS NULL"
        ) as cur:
            row = await cur.fetchone()
            print(f"transcribed rows with NULL path: {row[0]}")
        async with db.execute(
            "SELECT COUNT(*) FROM audio_chunks WHERE status='transcribed' AND path IS NOT NULL"
        ) as cur:
            row = await cur.fetchone()
            print(f"transcribed rows STILL with path (should be 0): {row[0]}")
        async with db.execute(
            "SELECT status, COUNT(*) FROM audio_chunks "
            "WHERE status IN ('recorded','no_audio','unavailable','transcription_failed') "
            "AND path IS NOT NULL GROUP BY status"
        ) as cur:
            print("untranscribed-with-path (should be untouched):")
            async for r in cur:
                print(f"  {r[0]}: {r[1]}")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
