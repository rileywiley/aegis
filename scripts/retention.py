#!/usr/bin/env python3
"""Aegis data retention report and cleanup.

Usage:
    python scripts/retention.py                # Dry-run: report what would be affected
    python scripts/retention.py --execute      # Actually archive/clean old data

Retention tiers (from config):
    - Hot  (default 90 days):  Items remain fully active in pipeline
    - Warm (default 365 days): Items kept for search but excluded from active processing
    - Cold (365+ days):        Candidates for deletion (only with --execute)
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from rich.console import Console
from rich.table import Table

console = Console()


async def _run_report(execute: bool) -> None:
    from aegis.config import get_settings
    from aegis.db.engine import async_session_factory
    from sqlalchemy import text

    settings = get_settings()
    hot_days = settings.retention_hot_days
    warm_days = settings.retention_warm_days

    now = datetime.now(timezone.utc)
    hot_cutoff = now - timedelta(days=hot_days)
    warm_cutoff = now - timedelta(days=warm_days)

    # Tables and their date columns to check
    targets = [
        ("meetings", "start_time"),
        ("emails", "datetime"),
        ("chat_messages", "datetime"),
        ("action_items", "created"),
        ("decisions", "datetime"),
        ("commitments", "created"),
        ("email_asks", "created"),
        ("chat_asks", "created"),
        ("briefings", "generated_at"),
    ]

    table = Table(title=f"Retention Report (hot={hot_days}d, warm={warm_days}d)")
    table.add_column("Table", style="cyan")
    table.add_column("Total", justify="right")
    table.add_column(f"Hot (<{hot_days}d)", justify="right", style="green")
    table.add_column(f"Warm ({hot_days}-{warm_days}d)", justify="right", style="yellow")
    table.add_column(f"Cold (>{warm_days}d)", justify="right", style="red")

    async with async_session_factory() as session:
        for tbl, col in targets:
            try:
                # Total count
                result = await session.execute(text(f"SELECT COUNT(*) FROM {tbl}"))
                total = result.scalar() or 0

                # Hot (within hot_days)
                result = await session.execute(
                    text(f"SELECT COUNT(*) FROM {tbl} WHERE {col} >= :cutoff"),
                    {"cutoff": hot_cutoff},
                )
                hot = result.scalar() or 0

                # Warm (between hot and warm cutoffs)
                result = await session.execute(
                    text(
                        f"SELECT COUNT(*) FROM {tbl} "
                        f"WHERE {col} < :hot_cutoff AND {col} >= :warm_cutoff"
                    ),
                    {"hot_cutoff": hot_cutoff, "warm_cutoff": warm_cutoff},
                )
                warm = result.scalar() or 0

                # Cold (older than warm_days)
                result = await session.execute(
                    text(f"SELECT COUNT(*) FROM {tbl} WHERE {col} < :cutoff"),
                    {"cutoff": warm_cutoff},
                )
                cold = result.scalar() or 0

                table.add_row(tbl, str(total), str(hot), str(warm), str(cold))

            except Exception as e:
                table.add_row(tbl, "[dim]error[/dim]", "", "", str(e)[:40])

    console.print(table)

    if not execute:
        console.print()
        console.print(
            "[yellow]Dry-run mode.[/yellow] "
            "No data was modified. Use --execute to apply retention."
        )
    else:
        console.print()
        console.print(
            "[yellow]Execute mode is not yet implemented.[/yellow] "
            "Future: will archive cold-tier items and remove embeddings from warm-tier."
        )


def main() -> None:
    execute = "--execute" in sys.argv
    asyncio.run(_run_report(execute))


if __name__ == "__main__":
    main()
