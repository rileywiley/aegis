#!/usr/bin/env python3
"""Seed the Aegis database with real calendar events + fake transcripts for Phase 1 validation.

Usage:
    python scripts/seed_test_data.py            # seed the database
    python scripts/seed_test_data.py --dry-run  # preview without writing
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.table import Table

# Ensure project root is on sys.path
sys.path.insert(0, ".")

from aegis.config import get_settings  # noqa: E402
from aegis.db.engine import async_session_factory, engine  # noqa: E402
from aegis.db.models import Meeting  # noqa: E402
from aegis.db.repositories import upsert_meeting  # noqa: E402
from aegis.ingestion.calendar_sync import CalendarSync  # noqa: E402
from aegis.ingestion.graph_client import GraphClient  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

console = Console()
logger = logging.getLogger("aegis.seed")

# ═══════════════════════════════════════════════════════════
# SAMPLE TRANSCRIPTS & SUMMARIES
# ═══════════════════════════════════════════════════════════

SAMPLE_TRANSCRIPTS = {
    "standup": """James: Morning everyone. Let's do updates.
Sarah: I finished the ALB config yesterday. Staging is stable now. 502s are gone.
James: Nice work. Derek, where are we on the cost projections for the board deck?
Derek: I have a draft. Should be ready by Wednesday.
James: The Phase 2 migration timeline is still blocked on DBA availability. I'll resolve that this week.
Sarah: I can help if you need another pair of hands on the Aurora setup.
James: Thanks. Let's regroup Thursday.""",

    "series b": """You: David, thanks for sending the redline Friday.
David: Happy to. I flagged three sections where we need to push back hard.
You: The liquidation preference is the most important. Let's address that first.
David: Agreed. Their 2x participating preferred is unusual for this stage.
You: Push for 1x non-participating. If they won't move, I'll escalate to the partner.
David: I'll draft the response and send it for your review by Thursday.
You: Perfect. Also confirm the investor dinner attendee list with Tom before May 2.""",

    "budget": """Lisa: The revised Q3 marketing budget is $310K, up from the original $240K.
You: Walk me through the increase.
Lisa: Digital spend is up $45K. Events are up $25K. I've included the breakdown.
Derek: This exceeds our Q3 allocation by 29%. We need to flag this to finance.
You: I want to review the digital breakdown before approving. Send me the line items.
Lisa: I'll have it to you by end of day.
Derek: If approved, I'll need to revise our Q3 forecast.""",

    "steering": """You: Let's review Phase 1 completion.
James: Phase 1 is signed off. All services migrated, no rollbacks needed.
Derek: I'm concerned about staging cost overruns — we're $8K over projection.
Anika: What's the impact on product roadmap timelines?
James: Phase 2 DBA issue is blocking the data migration. We may slip 2 weeks.
Anika: That pushes the feature flag rollout into next quarter.
You: Let's revisit the timeline at next steering. Derek, quantify the staging overruns by Apr 14.
James: I'll have the Phase 2 mitigation plan ready for next meeting.""",

    "1:1": """You: How are you doing overall?
Lisa: Honestly a bit stretched. The Q3 campaign launch is consuming most of my time.
You: Is the team bandwidth the issue, or something else?
Lisa: Team bandwidth. We're running two campaigns in parallel and Sarah is out next week.
You: What would help most — a contractor, reprioritization, or pushing a deadline?
Lisa: Contractor would be the fastest unblock. 6 weeks at 20 hours/week.
You: Put together a proposal with cost and I'll approve this week.
Lisa: Thanks. I'll send it tomorrow.""",
}

SAMPLE_SUMMARIES = {
    "standup": "Sarah confirmed ALB fix; staging stable. Derek's cost projections due Wednesday. James blocked on DBA for Phase 2; will resolve this week.",
    "series b": "Reviewed term sheet redline with David. Liquidation preference is top priority. David drafting response by Thursday. Investor dinner list needed by May 2.",
    "budget": "Lisa proposed Q3 marketing budget increase from $240K to $310K. Derek flagged 29% overage. User requested digital breakdown before approving.",
    "steering": "Phase 1 signed off. Staging cost overruns $8K above projection. Phase 2 blocked on DBA; potential 2-week slip. Derek to quantify overruns by Apr 14.",
    "1:1": "Lisa stretched by Q3 campaign launch. Sarah out next week. Lisa to send contractor proposal (6 weeks, 20 hrs/week) for user's approval this week.",
}


def match_sample(meeting_title: str) -> tuple[str | None, str | None]:
    """Returns (transcript, summary) if matched, (None, None) otherwise."""
    title_lower = meeting_title.lower()
    for keyword, transcript in SAMPLE_TRANSCRIPTS.items():
        if keyword in title_lower:
            return transcript, SAMPLE_SUMMARIES[keyword]
    return None, None


# ═══════════════════════════════════════════════════════════
# MAIN LOGIC
# ═══════════════════════════════════════════════════════════


async def seed(dry_run: bool = False) -> None:
    settings = get_settings()
    now_utc = datetime.now(timezone.utc)

    # ── Step 1: Fetch real calendar events ────────────────
    console.print("\n[bold cyan]Step 1:[/] Fetching calendar events from Microsoft Graph...")

    try:
        graph = GraphClient()
    except Exception as e:
        console.print(f"[bold red]Error initializing GraphClient:[/] {e}")
        console.print("Run the setup wizard first: python scripts/setup_graph.py")
        return

    # Last 7 days + today + next 2 days
    start_dt = (now_utc - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = (now_utc + timedelta(days=2)).replace(hour=23, minute=59, second=59, microsecond=0)

    start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        events = await graph.get_calendar_events(start_str, end_str)
    except Exception as e:
        console.print(f"[bold red]Graph API error:[/] {e}")
        console.print("Your OAuth token may have expired. Re-authenticate and try again.")
        return

    console.print(f"  Fetched [bold]{len(events)}[/] raw events from Graph API")

    # ── Step 2: Filter + upsert meetings ──────────────────
    console.print("\n[bold cyan]Step 2:[/] Filtering and upserting meetings...")

    sync = CalendarSync(graph)
    exclusion_keywords = [kw.lower() for kw in settings.exclusion_keywords_list]

    filtered_count = 0
    upserted_count = 0
    meetings_inserted: list[dict] = []  # track for transcript injection

    async with async_session_factory() as session:
        for event in events:
            if sync._should_skip(event, exclusion_keywords):
                filtered_count += 1
                continue

            meeting_data = sync._event_to_meeting_data(event)

            if dry_run:
                console.print(
                    f"  [dim]DRY-RUN would upsert:[/] {meeting_data['title']} "
                    f"({meeting_data['start_time'].strftime('%b %d %H:%M')})"
                )
            else:
                meeting = await upsert_meeting(session, meeting_data)
                # Sync attendees
                attendees = event.get("attendees", [])
                await sync._sync_attendees(session, meeting.id, attendees)

            meetings_inserted.append(meeting_data)
            upserted_count += 1

        console.print(
            f"  [bold]{upserted_count}[/] meetings upserted, "
            f"[dim]{filtered_count}[/] filtered out"
        )

        # ── Step 3: Inject fake transcripts into past meetings ─
        console.print("\n[bold cyan]Step 3:[/] Injecting fake transcripts into past meetings...")

        transcript_count = 0
        no_audio_count = 0

        if not dry_run:
            # Query all past meetings that don't have transcripts yet
            stmt = (
                select(Meeting)
                .where(
                    Meeting.end_time < now_utc,
                    Meeting.is_excluded.is_(False),
                )
                .order_by(Meeting.start_time)
            )
            result = await session.execute(stmt)
            past_meetings = list(result.scalars().all())

            for meeting in past_meetings:
                transcript, summary = match_sample(meeting.title)
                if transcript:
                    await session.execute(
                        update(Meeting)
                        .where(Meeting.id == meeting.id)
                        .values(
                            transcript_text=transcript,
                            transcript_status="captured",
                            summary=summary,
                        )
                    )
                    transcript_count += 1
                    console.print(
                        f"  [green]+[/] Transcript injected: {meeting.title}"
                    )
                else:
                    await session.execute(
                        update(Meeting)
                        .where(Meeting.id == meeting.id)
                        .values(transcript_status="no_audio")
                    )
                    no_audio_count += 1
                    console.print(
                        f"  [dim]-[/] No match (no_audio): {meeting.title}"
                    )

            await session.commit()
        else:
            # Dry-run: just match against the meetings we would have inserted
            for md in meetings_inserted:
                if md["end_time"] < now_utc:
                    transcript, _ = match_sample(md["title"])
                    if transcript:
                        transcript_count += 1
                        console.print(
                            f"  [dim]DRY-RUN would inject transcript:[/] {md['title']}"
                        )
                    else:
                        no_audio_count += 1
                        console.print(
                            f"  [dim]DRY-RUN would mark no_audio:[/] {md['title']}"
                        )

        # ── Step 4: Create HR test meeting ────────────────────
        console.print("\n[bold cyan]Step 4:[/] Creating test meeting (HR keyword exclusion)...")

        hr_event_id = "seed-test-hr-confidential"
        hr_meeting_data = {
            "title": "Confidential HR discussion",
            "start_time": now_utc - timedelta(hours=3),
            "end_time": now_utc - timedelta(hours=2),
            "duration": 60,
            "status": "completed",
            "meeting_type": "virtual",
            "calendar_event_id": hr_event_id,
            "is_excluded": True,
            "transcript_status": "no_audio",
        }

        if dry_run:
            console.print(
                "  [dim]DRY-RUN would insert:[/] Confidential HR discussion (is_excluded=True)"
            )
            hr_excluded = True
        else:
            hr_meeting = await upsert_meeting(session, hr_meeting_data)
            # Verify exclusion
            refreshed = await session.get(Meeting, hr_meeting.id)
            hr_excluded = refreshed.is_excluded if refreshed else False
            status_str = "[green]excluded[/]" if hr_excluded else "[red]NOT excluded[/]"
            console.print(f"  Test meeting created: {status_str}")

    # ── Step 5: Summary report ────────────────────────────
    console.print()

    table = Table(title="SEED COMPLETE" + (" (DRY RUN)" if dry_run else ""), show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Real calendar events fetched", str(len(events)))
    table.add_row(
        "Meetings inserted/updated",
        f"{upserted_count} ({filtered_count} filtered out)",
    )
    table.add_row("Past meetings with fake transcript", str(transcript_count))
    table.add_row("Past meetings marked no_audio", str(no_audio_count))
    check = "[green]excluded[/]" if hr_excluded else "[red]NOT excluded[/]"
    table.add_row("Test meeting (HR confidential)", check)

    console.print(table)
    console.print("\nOpen [bold underline]http://localhost:8000[/] to verify.\n")

    await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Seed Aegis with test data for Phase 1 validation")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to database")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(seed(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
