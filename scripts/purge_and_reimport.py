#!/usr/bin/env python3
"""Purge extracted data and reimport from Graph API.

Preserves: people, departments, teams structure, voice_profile, admin_settings.
Purges: all extracted entities, emails, chat_messages, meetings, workstreams, etc.
Then reimports from Graph API (90 days email, 60 days calendar, 30 days Teams).
"""

import asyncio
import sys
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


async def main():
    from aegis.config import get_settings
    settings = get_settings()

    print("""
╔══════════════════════════════════════════════════════════════╗
║  AEGIS DATA PURGE & REIMPORT                                ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  This will DELETE all data from:                             ║
║    decisions, action_items, commitments                      ║
║    email_asks, chat_asks                                     ║
║    emails, chat_messages, meetings, meeting_attendees         ║
║    workstream_items, workstream_stakeholders                  ║
║    workstream_milestones                                      ║
║    topics, meeting_topics, email_topics, chat_message_topics  ║
║    briefings, drafts, dashboard_cache, chat_sessions          ║
║    sentiment_aggregations, attachments                        ║
║    system_health, llm_usage                                   ║
║                                                              ║
║  PRESERVED:                                                  ║
║    people (needs_review reset, interaction_count cleared)     ║
║    departments                                                ║
║    workstreams (items cleared, structure kept)                 ║
║    teams, team_channels, team_memberships                     ║
║    voice_profile                                              ║
║    admin_settings                                             ║
║                                                              ║
║  After purge, reimport runs:                                 ║
║    90 days email, 60 days calendar, 30 days Teams messages   ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
""")

    if not settings.user_email:
        print("ERROR: USER_EMAIL not set in .env. Required for directional tabs.")
        print("Add USER_EMAIL=your.email@hawthornehealth.com to .env")
        sys.exit(1)

    confirmation = input("Type 'PURGE' to confirm: ").strip()
    if confirmation != "PURGE":
        print("Aborted.")
        sys.exit(0)

    from sqlalchemy import text
    from aegis.db.engine import async_session_factory

    print("\n[1/4] Purging extracted data...")
    async with async_session_factory() as session:
        # Order matters for foreign keys — delete children first
        tables_to_truncate = [
            "chat_message_topics", "email_topics", "meeting_topics",
            "workstream_items", "workstream_stakeholders", "workstream_milestones",
            "chat_asks", "email_asks",
            "action_items", "decisions", "commitments",
            "attachments", "drafts", "briefings",
            "dashboard_cache", "chat_sessions",
            "sentiment_aggregations", "system_health", "llm_usage",
            "meeting_attendees",
            "chat_messages", "emails", "meetings",
            "topics",
        ]
        for table in tables_to_truncate:
            await session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
            print(f"  Truncated {table}")

        # Reset people but keep them
        await session.execute(text(
            "UPDATE people SET needs_review = true, interaction_count = 0, "
            "llm_suggestion = NULL"
        ))
        print("  Reset people (needs_review=true, interaction_count=0)")

        await session.commit()
    print("  Purge complete.")

    print("\n[2/4] Reimporting from Graph API...")
    from aegis.ingestion.graph_client import GraphClient
    from aegis.ingestion.calendar_sync import CalendarSync
    from aegis.ingestion.email_poller import EmailPoller
    from aegis.ingestion.teams_poller import TeamsPoller

    graph = GraphClient()

    # Calendar
    print("  Syncing calendar...")
    calendar_sync = CalendarSync(graph)
    async with async_session_factory() as session:
        try:
            count = await calendar_sync.sync(session)
            print(f"  Calendar: {count} meetings synced")
        except Exception as e:
            print(f"  Calendar sync failed: {e}")

    # Email
    print("  Importing emails...")
    async with async_session_factory() as session:
        try:
            email_poller = EmailPoller(graph)
            count = await email_poller.poll(session)
            print(f"  Email: {count} emails imported")
        except Exception as e:
            print(f"  Email import failed: {e}")

    # Teams: sync structure + messages
    print("  Syncing Teams structure + messages...")
    async with async_session_factory() as session:
        try:
            teams_poller = TeamsPoller(graph)
            count = await teams_poller.poll(session)
            print(f"  Teams: {count} messages imported")
        except Exception as e:
            print(f"  Teams import failed: {e}")

    print("\n[3/4] Summary")
    async with async_session_factory() as session:
        for table in ["emails", "meetings", "chat_messages", "people"]:
            result = await session.execute(text(f"SELECT count(*) FROM {table}"))
            count = result.scalar_one()
            print(f"  {table}: {count} rows")

    print("\n[4/4] Processing will begin on the next 30-minute cycle.")
    print("  The new extraction prompts will be used for all items.")
    print("  Wait 30-60 minutes, then check the Command Center.")
    print("\n  Done.")


if __name__ == "__main__":
    asyncio.run(main())
