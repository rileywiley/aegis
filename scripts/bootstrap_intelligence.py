#!/usr/bin/env python3
"""Bootstrap all Phase 4 intelligence services — run once after deployment.

Triggers: morning briefing, meeting prep, voice profile, sentiment,
readiness cache, draft generation, and dashboard cache refresh.

Usage:
    python scripts/bootstrap_intelligence.py
"""

import asyncio
import logging
import sys

from rich.console import Console

sys.path.insert(0, ".")

from aegis.db.engine import async_session_factory, engine  # noqa: E402

console = Console()
logger = logging.getLogger("aegis.bootstrap")


async def bootstrap():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    # ── 1. Morning briefing ─────────────────────────────
    console.print("\n[bold cyan]1.[/] Generating morning briefing...")
    try:
        from aegis.intelligence.briefings import generate_morning_briefing
        async with async_session_factory() as session:
            content = await generate_morning_briefing(session)
            console.print(f"  [green]Done[/] — {len(content)} chars")
    except Exception as e:
        console.print(f"  [red]Failed:[/] {e}")

    # ── 2. Voice profile ────────────────────────────────
    console.print("\n[bold cyan]2.[/] Learning voice profile from sent emails...")
    try:
        from aegis.intelligence.voice_profile import learn_voice
        async with async_session_factory() as session:
            await learn_voice(session)
            console.print("  [green]Done[/]")
    except Exception as e:
        console.print(f"  [red]Failed:[/] {e}")

    # ── 3. Sentiment aggregation ────────────────────────
    console.print("\n[bold cyan]3.[/] Computing sentiment aggregations...")
    try:
        from aegis.intelligence.sentiment import compute_sentiment_aggregations
        async with async_session_factory() as session:
            stats = await compute_sentiment_aggregations(session)
            console.print(f"  [green]Done[/] — {stats}")
    except Exception as e:
        console.print(f"  [red]Failed:[/] {e}")

    # ── 4. Draft generation (stale nudges + recaps) ─────
    console.print("\n[bold cyan]4.[/] Generating drafts for stale items...")
    try:
        from aegis.intelligence.draft_generator import generate_stale_nudges, generate_meeting_recaps
        async with async_session_factory() as session:
            nudges = await generate_stale_nudges(session)
            recaps = await generate_meeting_recaps(session)
            console.print(f"  [green]Done[/] — {nudges} nudges, {recaps} recaps")
    except Exception as e:
        console.print(f"  [red]Failed:[/] {e}")

    # ── 5. Dashboard cache refresh ──────────────────────
    console.print("\n[bold cyan]5.[/] Refreshing dashboard cache...")
    try:
        from aegis.web.routes.dashboard import refresh_dashboard_cache
        await refresh_dashboard_cache()
        console.print("  [green]Done[/]")
    except Exception as e:
        console.print(f"  [red]Failed:[/] {e}")

    # ── 6. Processing cycle (triage + extraction + embeddings) ──
    console.print("\n[bold cyan]6.[/] Running processing cycle (triage + extraction + embeddings)...")
    try:
        from aegis.main import _run_processing_cycle
        await _run_processing_cycle()
        console.print("  [green]Done[/]")
    except Exception as e:
        console.print(f"  [red]Failed:[/] {e}")

    # ── 7. Org inference ────────────────────────────────
    console.print("\n[bold cyan]7.[/] Running org inference...")
    try:
        from aegis.processing.org_inference import infer_org_structure
        async with async_session_factory() as session:
            stats = await infer_org_structure(session)
            console.print(f"  [green]Done[/] — {stats}")
    except Exception as e:
        console.print(f"  [red]Failed:[/] {e}")

    console.print("\n[bold green]Bootstrap complete.[/] Re-run verify_phase4.py to check results.\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(bootstrap())
