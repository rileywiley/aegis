#!/usr/bin/env python3
"""Aegis Phase 3+4+5 Full System Verification Script — read-only diagnostic against the live database."""

import argparse
import asyncio
import glob as glob_mod
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

from sqlalchemy import text

from aegis.config import get_settings
from aegis.db.engine import async_session_factory

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ── Globals set by CLI args ──────────────────────────────────────
VERBOSE = False
FIX_SUGGESTIONS = False

# ── Result tracking ──────────────────────────────────────────────
# Each section appends its result here: ("PASS"/"WARNING"/"FAIL", section_num, title, detail)
results: list[tuple[str, int, str, str]] = []


def status_icon(status: str) -> str:
    return {"PASS": "[green]PASS[/green]", "WARNING": "[yellow]WARNING[/yellow]", "FAIL": "[red]FAIL[/red]"}[status]


def time_ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds} sec ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} hours ago"
    return f"{seconds // 86400} days ago"


def section_header(num: int, title: str) -> None:
    console.print()
    console.rule(f"[bold]SECTION {num}: {title}[/bold]")
    console.print()


def record_result(status: str, section: int, title: str, detail: str = "") -> None:
    results.append((status, section, title, detail))


def suggest(msg: str) -> None:
    if FIX_SUGGESTIONS:
        console.print(f"  [dim italic]Fix: {msg}[/dim italic]")


# ══════════════════════════════════════════════════════════════════
# Helper: find the user's person record
# ══════════════════════════════════════════════════════════════════

async def find_user_person_id(session) -> int | None:
    """Try to find the user's person record by matching known emails."""
    candidate_emails: list[str] = ["delemos.ricardo@gmail.com"]

    for email in candidate_emails:
        row = await session.execute(
            text("SELECT id, name, email FROM people WHERE LOWER(email) = LOWER(:email) LIMIT 1"),
            {"email": email},
        )
        result = row.first()
        if result:
            console.print(f"  [dim]User identified: {result.name} (id={result.id}, email={result.email})[/dim]")
            return result.id

    # Fallback: look for a person who is the most frequent sender
    row = await session.execute(
        text("""
            SELECT p.id, p.name, p.email
            FROM people p
            JOIN emails e ON e.sender_id = p.id
            GROUP BY p.id, p.name, p.email
            ORDER BY COUNT(*) DESC LIMIT 1
        """)
    )
    result = row.first()
    if result:
        console.print(f"  [dim]User guessed (top sender): {result.name} (id={result.id}, email={result.email})[/dim]")
        return result.id

    return None


# ══════════════════════════════════════════════════════════════════
#                    PART A: PHASE 3 RE-CHECKS
# ══════════════════════════════════════════════════════════════════

# ── SECTION 1: SERVICE HEALTH (Phase 3 services) ────────────────

async def check_service_health(session) -> None:
    section_header(1, "SERVICE HEALTH (Phase 3 services)")

    rows = await session.execute(
        text("""
            SELECT service, status, last_success, last_error, last_error_message,
                   items_processed_last_hour
            FROM system_health
            WHERE service IN (
                'email_poller', 'teams_poller', 'calendar_sync',
                'triage_batch', 'workstream_detector', 'extraction_pipeline'
            )
            ORDER BY service
        """)
    )
    services = rows.fetchall()

    expected = {'email_poller', 'teams_poller', 'calendar_sync',
                'triage_batch', 'workstream_detector', 'extraction_pipeline'}
    found = {s.service for s in services}
    missing = expected - found

    if not services:
        console.print("  [red]No Phase 3 services registered in system_health table[/red]")
        record_result("FAIL", 1, "Service Health", "No Phase 3 services in system_health")
        suggest("Ensure all pollers call update_system_health() after each cycle.")
        return

    has_down = False
    has_degraded = False

    table = Table(show_header=True, header_style="bold")
    table.add_column("Status", width=4)
    table.add_column("Service", min_width=22)
    table.add_column("State", min_width=10)
    table.add_column("Last Success", min_width=16)
    table.add_column("Items/hr", justify="right", min_width=8)
    table.add_column("Recent Error", max_width=40)

    for svc in services:
        icon = {"healthy": "[green]OK[/green]", "degraded": "[yellow]!![/yellow]", "down": "[red]XX[/red]"}.get(
            svc.status, "[dim]??[/dim]"
        )
        if svc.status == "down":
            has_down = True
        elif svc.status == "degraded":
            has_degraded = True

        error_msg = ""
        if svc.last_error:
            now = datetime.now(timezone.utc)
            le = svc.last_error
            if le.tzinfo is None:
                from zoneinfo import ZoneInfo
                le = le.replace(tzinfo=ZoneInfo("UTC"))
            if (now - le).total_seconds() < 3600 and svc.last_error_message:
                error_msg = svc.last_error_message[:40]

        table.add_row(
            icon, svc.service, svc.status or "unknown",
            time_ago(svc.last_success),
            str(svc.items_processed_last_hour or 0), error_msg,
        )

    console.print(table)

    if missing:
        console.print(f"  [yellow]Missing services: {', '.join(sorted(missing))}[/yellow]")

    if has_down or missing:
        record_result("FAIL", 1, "Service Health",
                       f"Services down or missing: {', '.join(sorted(missing)) if missing else 'see table'}")
        suggest("Check logs for down services. Restart the relevant poller.")
    elif has_degraded:
        record_result("WARNING", 1, "Service Health", "One or more services degraded")
        suggest("Service may recover on its own. Check last_error_message for details.")
    else:
        record_result("PASS", 1, "Service Health", "All Phase 3 services healthy")


# ── SECTION 2: EMAIL INGESTION ──────────────────────────────────

async def check_email_ingestion(session) -> None:
    section_header(2, "EMAIL INGESTION")

    row = await session.execute(text("SELECT COUNT(*) FROM emails"))
    total = row.scalar()

    if total == 0:
        console.print("  [red]Emails table is empty[/red]")
        record_result("FAIL", 2, "Email Ingestion", "emails table is empty")
        suggest("Run the email poller: check ingestion/email_poller.py and ensure Graph API auth is working.")
        return

    rows = await session.execute(
        text("SELECT email_class, COUNT(*) as cnt FROM emails GROUP BY email_class ORDER BY cnt DESC")
    )
    classes = rows.fetchall()
    class_dict = {r.email_class: r.cnt for r in classes}

    console.print(f"  Total emails ingested: [bold]{total}[/bold]")
    for cls, cnt in class_dict.items():
        pct = cnt / total * 100 if total else 0
        console.print(f"    {cls or 'NULL':20s} {cnt:>6d}  ({pct:.1f}%)")

    human_count = class_dict.get("human", 0)
    distinct_classes = len([c for c in class_dict if c is not None])
    human_pct = human_count / total * 100 if total else 0

    if total > 100 and distinct_classes >= 3 and 20 <= human_pct <= 50:
        record_result("PASS", 2, "Email Ingestion", f"{total} emails, human={human_pct:.0f}%")
    elif 50 <= total <= 100:
        record_result("WARNING", 2, "Email Ingestion", f"Only {total} emails (expected >100)")
    elif total > 100 and (distinct_classes < 3 or human_pct < 20 or human_pct > 50):
        record_result("WARNING", 2, "Email Ingestion",
                       f"{total} emails, {distinct_classes} classes, human={human_pct:.0f}%")
        suggest("Check email_poller.py noise classification logic.")
    elif total < 50:
        record_result("FAIL", 2, "Email Ingestion", f"Only {total} emails")
        suggest("Email poller may be broken. Check Graph API auth and email_poller.py.")
    else:
        record_result("PASS", 2, "Email Ingestion", f"{total} emails, {distinct_classes} classes")

    if VERBOSE:
        console.print()
        console.print("  [dim]Sample automated emails:[/dim]")
        rows = await session.execute(
            text("SELECT subject, email_class FROM emails WHERE email_class = 'automated' ORDER BY datetime DESC LIMIT 5")
        )
        for r in rows.fetchall():
            console.print(f"    [dim]- {(r.subject or '(no subject)')[:70]}[/dim]")

        console.print("  [dim]Sample human emails:[/dim]")
        rows = await session.execute(
            text("SELECT subject, email_class FROM emails WHERE email_class = 'human' ORDER BY datetime DESC LIMIT 5")
        )
        for r in rows.fetchall():
            console.print(f"    [dim]- {(r.subject or '(no subject)')[:70]}[/dim]")


# ── SECTION 3: EMAIL TRIAGE ─────────────────────────────────────

async def check_email_triage(session) -> None:
    section_header(3, "EMAIL TRIAGE")

    rows = await session.execute(
        text("""
            SELECT triage_class, COUNT(*) as cnt, ROUND(AVG(triage_score)::numeric, 2) as avg_score
            FROM emails WHERE email_class = 'human' GROUP BY triage_class
        """)
    )
    triage = rows.fetchall()
    triage_dict = {r.triage_class: (r.cnt, r.avg_score) for r in triage}

    total_human = sum(cnt for cnt, _ in triage_dict.values())

    if total_human == 0:
        console.print("  [red]No human emails to evaluate triage on[/red]")
        record_result("FAIL", 3, "Email Triage", "No human emails found")
        return

    all_null = all(k is None for k in triage_dict.keys())
    if all_null:
        console.print("  [red]triage_class is NULL for all human emails -- triage not running[/red]")
        record_result("FAIL", 3, "Email Triage", "Triage not running")
        suggest("Check processing/triage.py is being called after email polling.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Triage Class")
    table.add_column("Count", justify="right")
    table.add_column("% of Human", justify="right")
    table.add_column("Avg Score", justify="right")

    for cls in ["substantive", "contextual", "noise", None]:
        if cls in triage_dict:
            cnt, avg = triage_dict[cls]
            pct = cnt / total_human * 100 if total_human else 0
            table.add_row(str(cls) if cls else "NULL", str(cnt), f"{pct:.1f}%", str(avg or ""))

    console.print(table)

    sub_count = triage_dict.get("substantive", (0, 0))[0]
    sub_pct = sub_count / total_human * 100 if total_human else 0
    classes_present = len([k for k in triage_dict if k is not None])

    if classes_present >= 3 and 20 <= sub_pct <= 50:
        record_result("PASS", 3, "Email Triage", f"Substantive: {sub_pct:.0f}% of human emails")
    elif sub_pct > 70:
        record_result("WARNING", 3, "Email Triage", f"Substantive too high: {sub_pct:.0f}% (>70% skew)")
        suggest("Increase triage_substantive_threshold in config.")
    elif sub_pct < 10 and classes_present > 0:
        record_result("WARNING", 3, "Email Triage", f"Substantive too low: {sub_pct:.0f}%")
        suggest("Decrease triage_substantive_threshold in config.")
    else:
        record_result("PASS", 3, "Email Triage", f"Substantive: {sub_pct:.0f}% of human emails")

    if VERBOSE:
        console.print()
        console.print("  [dim]Sample substantive emails:[/dim]")
        rows = await session.execute(
            text("""
                SELECT subject, triage_score FROM emails
                WHERE email_class = 'human' AND triage_class = 'substantive'
                ORDER BY datetime DESC LIMIT 3
            """)
        )
        for r in rows.fetchall():
            console.print(f"    [dim]- [{r.triage_score}] {(r.subject or '(no subject)')[:60]}[/dim]")

        console.print("  [dim]Sample contextual emails:[/dim]")
        rows = await session.execute(
            text("""
                SELECT subject, triage_score FROM emails
                WHERE email_class = 'human' AND triage_class = 'contextual'
                ORDER BY datetime DESC LIMIT 3
            """)
        )
        for r in rows.fetchall():
            console.print(f"    [dim]- [{r.triage_score}] {(r.subject or '(no subject)')[:60]}[/dim]")


# ── SECTION 4: EMAIL EXTRACTION & ASK DIRECTIONALITY ────────────

async def check_email_extraction(session) -> None:
    section_header(4, "EMAIL EXTRACTION & ASK DIRECTIONALITY")

    row = await session.execute(text("SELECT COUNT(*) FROM email_asks"))
    total_asks = row.scalar()

    if total_asks == 0:
        console.print("  [red]email_asks table is empty[/red]")
        record_result("FAIL", 4, "Email Extraction", "email_asks table is empty")
        suggest("Check processing/email_extractor.py -- extraction may not be running on substantive emails.")
        return

    row = await session.execute(
        text("""
            SELECT COUNT(*) FILTER (WHERE requester_id IS NOT NULL) as has_requester,
                   COUNT(*) FILTER (WHERE target_id IS NOT NULL) as has_target,
                   COUNT(*) FILTER (WHERE requester_id IS NOT NULL AND target_id IS NOT NULL) as has_both
            FROM email_asks
        """)
    )
    r = row.first()
    has_both_pct = r.has_both / total_asks * 100 if total_asks else 0

    console.print(f"  Total email asks: [bold]{total_asks}[/bold]")
    console.print(f"    With requester: {r.has_requester} ({r.has_requester / total_asks * 100:.0f}%)")
    console.print(f"    With target:    {r.has_target} ({r.has_target / total_asks * 100:.0f}%)")
    console.print(f"    With both:      {r.has_both} ({has_both_pct:.0f}%)")

    status = "PASS"
    detail = f"{total_asks} asks, {has_both_pct:.0f}% have full directionality"

    if total_asks < 10:
        status = "WARNING"
        detail = f"Only {total_asks} asks (expected >10)"
    elif has_both_pct < 50:
        status = "WARNING"
        detail = f"Only {has_both_pct:.0f}% of asks have both requester and target"
        suggest("Check resolver.py -- entity resolution may not be linking people to asks.")

    # Directionality check for the user
    user_id = await find_user_person_id(session)
    if user_id:
        row_to = await session.execute(
            text("SELECT COUNT(*) FROM email_asks WHERE target_id = :uid"), {"uid": user_id}
        )
        row_from = await session.execute(
            text("SELECT COUNT(*) FROM email_asks WHERE requester_id = :uid"), {"uid": user_id}
        )
        asks_to_user = row_to.scalar()
        asks_from_user = row_from.scalar()
        console.print(f"\n  Asks directed at you: [bold]{asks_to_user}[/bold]")
        console.print(f"  Asks you made:        [bold]{asks_from_user}[/bold]")

        if asks_to_user > 0 and asks_from_user > 0:
            detail += f"; user has {asks_to_user} inbound, {asks_from_user} outbound"
        elif asks_to_user == 0 and asks_from_user == 0:
            if status == "PASS":
                status = "WARNING"
            detail += "; user has 0 inbound and 0 outbound asks (unexpected)"
            suggest("User's person record may not be linked correctly to asks.")
    else:
        console.print("\n  [yellow]Could not identify user's person record[/yellow]")

    record_result(status, 4, "Email Extraction", detail)

    if VERBOSE:
        console.print()
        console.print("  [dim]Sample asks:[/dim]")
        rows = await session.execute(
            text("""
                SELECT ea.description, ea.urgency, ea.ask_type,
                       pr.name as requester, pt.name as target
                FROM email_asks ea
                LEFT JOIN people pr ON ea.requester_id = pr.id
                LEFT JOIN people pt ON ea.target_id = pt.id
                ORDER BY ea.created DESC LIMIT 5
            """)
        )
        for a in rows.fetchall():
            console.print(f"    [dim]- [{a.urgency}/{a.ask_type}] {(a.description or '')[:50]} "
                          f"(from: {a.requester or '?'} -> to: {a.target or '?'})[/dim]")


# ── SECTION 5: EMAIL THREAD RESOLUTION ──────────────────────────

async def check_thread_resolution(session) -> None:
    section_header(5, "EMAIL THREAD RESOLUTION")

    row = await session.execute(
        text("""
            SELECT COUNT(*) FILTER (WHERE status = 'completed' AND resolved_by_email_id IS NOT NULL) as resolved,
                   COUNT(*) FILTER (WHERE status = 'open') as still_open,
                   COUNT(*) as total
            FROM email_asks
        """)
    )
    r = row.first()

    if r.total == 0:
        console.print("  [red]No email asks at all[/red]")
        record_result("FAIL", 5, "Thread Resolution", "No asks to evaluate")
        return

    console.print(f"  Total asks: [bold]{r.total}[/bold]")
    console.print(f"  Resolved by later email: [bold]{r.resolved}[/bold]")
    console.print(f"  Still open: [bold]{r.still_open}[/bold]")

    if r.resolved and r.resolved > 0:
        record_result("PASS", 5, "Thread Resolution", f"{r.resolved} asks resolved via thread analysis")
    elif r.still_open and r.still_open > 0:
        record_result("WARNING", 5, "Thread Resolution",
                       "Open asks exist but zero resolved by thread analysis")
        suggest("Check processing/thread_analyzer.py -- thread resolution may not be running.")
    else:
        record_result("WARNING", 5, "Thread Resolution", "Cannot assess thread resolution")

    if VERBOSE:
        console.print()
        thread_row = await session.execute(
            text("""
                SELECT thread_id FROM emails
                WHERE thread_id IS NOT NULL
                GROUP BY thread_id HAVING COUNT(*) > 1
                LIMIT 1
            """)
        )
        tid = thread_row.scalar()
        if tid:
            console.print(f"  [dim]Sample multi-email thread ({tid[:30]}...):[/dim]")
            emails_in_thread = await session.execute(
                text("""
                    SELECT subject, datetime, email_class, triage_class
                    FROM emails WHERE thread_id = :tid ORDER BY datetime
                """),
                {"tid": tid},
            )
            for e in emails_in_thread.fetchall():
                console.print(f"    [dim]{e.datetime} | {(e.subject or '(no subject)')[:50]} "
                              f"| class={e.email_class} triage={e.triage_class}[/dim]")


# ── SECTION 6: TEAMS INGESTION ──────────────────────────────────

async def check_teams_ingestion(session) -> None:
    section_header(6, "TEAMS INGESTION")

    row = await session.execute(text("SELECT COUNT(*) FROM chat_messages"))
    total = row.scalar()

    if total == 0:
        console.print("  [red]chat_messages table is empty[/red]")
        record_result("FAIL", 6, "Teams Ingestion", "chat_messages table is empty")
        suggest("Check ingestion/teams_poller.py and verify Graph API Chat.Read permission.")
        return

    rows = await session.execute(
        text("""
            SELECT source_type, COUNT(*) as total,
                   COUNT(*) FILTER (WHERE noise_filtered = true) as filtered,
                   COUNT(*) FILTER (WHERE noise_filtered = false) as kept
            FROM chat_messages GROUP BY source_type
        """)
    )
    types = rows.fetchall()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Source Type")
    table.add_column("Total", justify="right")
    table.add_column("Filtered", justify="right")
    table.add_column("Kept", justify="right")

    source_types_found = set()
    for t in types:
        source_types_found.add(t.source_type)
        table.add_row(t.source_type, str(t.total), str(t.filtered or 0), str(t.kept or 0))

    console.print(table)

    has_chat = "teams_chat" in source_types_found
    has_channel = "teams_channel" in source_types_found

    if has_chat and has_channel:
        chat_total = sum(t.total for t in types if t.source_type == "teams_chat")
        channel_total = sum(t.total for t in types if t.source_type == "teams_channel")
        if chat_total > 20 and channel_total > 20:
            record_result("PASS", 6, "Teams Ingestion", f"{total} messages across both source types")
        else:
            record_result("WARNING", 6, "Teams Ingestion",
                           f"Low volume: chat={chat_total}, channel={channel_total}")
    elif not has_chat and not has_channel:
        record_result("FAIL", 6, "Teams Ingestion", "No messages from either source type")
    else:
        missing_type = "teams_channel" if not has_channel else "teams_chat"
        record_result("WARNING", 6, "Teams Ingestion", f"Missing source type: {missing_type}")
        suggest("Check teams_poller.py -- may not be polling both chats and channels.")

    if VERBOSE:
        console.print()
        console.print("  [dim]Sample filtered messages (should be noise):[/dim]")
        rows = await session.execute(
            text("SELECT body_preview FROM chat_messages WHERE noise_filtered = true LIMIT 5")
        )
        for r in rows.fetchall():
            console.print(f"    [dim]- {(r.body_preview or '(empty)')[:70]}[/dim]")

        console.print("  [dim]Sample kept messages:[/dim]")
        rows = await session.execute(
            text("SELECT body_preview FROM chat_messages WHERE noise_filtered = false LIMIT 5")
        )
        for r in rows.fetchall():
            console.print(f"    [dim]- {(r.body_preview or '(empty)')[:70]}[/dim]")


# ── SECTION 7: TEAMS TRIAGE ─────────────────────────────────────

async def check_teams_triage(session) -> None:
    section_header(7, "TEAMS TRIAGE")

    row = await session.execute(
        text("SELECT COUNT(*) FROM chat_messages WHERE noise_filtered = false")
    )
    non_filtered = row.scalar()

    if non_filtered == 0:
        console.print("  [red]No non-filtered chat messages exist[/red]")
        record_result("FAIL", 7, "Teams Triage", "No non-filtered messages to triage")
        return

    rows = await session.execute(
        text("""
            SELECT triage_class, COUNT(*) as cnt
            FROM chat_messages WHERE noise_filtered = false GROUP BY triage_class
        """)
    )
    triage = rows.fetchall()
    triage_dict = {r.triage_class: r.cnt for r in triage}

    table = Table(show_header=True, header_style="bold")
    table.add_column("Triage Class")
    table.add_column("Count", justify="right")
    table.add_column("% of Kept", justify="right")

    for cls in ["substantive", "contextual", "noise", None]:
        if cls in triage_dict:
            cnt = triage_dict[cls]
            pct = cnt / non_filtered * 100 if non_filtered else 0
            table.add_row(str(cls) if cls else "NULL", str(cnt), f"{pct:.1f}%")

    console.print(table)

    null_count = triage_dict.get(None, 0)
    non_null_classes = [k for k in triage_dict if k is not None]

    if len(non_null_classes) > 0 and null_count < non_filtered:
        record_result("PASS", 7, "Teams Triage", f"{len(non_null_classes)} triage classes present")
    else:
        record_result("FAIL", 7, "Teams Triage", "All non-filtered messages have NULL triage_class")
        suggest("Check processing/triage.py -- ensure chat_messages are included in triage batch.")


# ── SECTION 8: CHAT ASKS EXTRACTION ─────────────────────────────

async def check_chat_asks(session) -> None:
    section_header(8, "CHAT ASKS EXTRACTION")

    row = await session.execute(
        text("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE requester_id IS NOT NULL) as has_requester,
                   COUNT(*) FILTER (WHERE target_id IS NOT NULL) as has_target
            FROM chat_asks
        """)
    )
    r = row.first()

    console.print(f"  Total chat asks: [bold]{r.total}[/bold]")

    if r.total == 0:
        row2 = await session.execute(
            text("SELECT COUNT(*) FROM chat_messages WHERE triage_class = 'substantive'")
        )
        substantive_count = row2.scalar()

        if substantive_count and substantive_count > 0:
            console.print(f"  [yellow]{substantive_count} substantive chat messages exist but no asks extracted[/yellow]")
            record_result("WARNING", 8, "Chat Asks", "Substantive chats exist but no asks extracted")
            suggest("Check processing/chat_extractor.py -- extraction may not be producing asks.")
        else:
            console.print("  [yellow]No substantive chat messages -- empty asks may be expected[/yellow]")
            record_result("WARNING", 8, "Chat Asks", "No substantive chats, so empty asks may be expected")
        return

    console.print(f"    With requester: {r.has_requester}")
    console.print(f"    With target:    {r.has_target}")

    if r.has_requester > 0 and r.has_target > 0:
        record_result("PASS", 8, "Chat Asks", f"{r.total} asks with directionality")
    else:
        record_result("WARNING", 8, "Chat Asks", f"{r.total} asks but missing directionality")
        suggest("Check chat_extractor.py and resolver.py for directionality assignment.")


# ── SECTION 9: TEAMS MEMBERSHIP & ORG ──────────────────────────

async def check_teams_membership(session) -> None:
    section_header(9, "TEAMS MEMBERSHIP & ORG STRUCTURE")

    row_teams = await session.execute(text("SELECT COUNT(*) FROM teams"))
    row_channels = await session.execute(text("SELECT COUNT(*) FROM team_channels"))
    row_members = await session.execute(text("SELECT COUNT(*) FROM team_memberships"))

    teams_count = row_teams.scalar()
    channels_count = row_channels.scalar()
    members_count = row_members.scalar()

    console.print(f"  Teams:       [bold]{teams_count}[/bold]")
    console.print(f"  Channels:    [bold]{channels_count}[/bold]")
    console.print(f"  Memberships: [bold]{members_count}[/bold]")

    if teams_count == 0:
        console.print("  [red]Teams table is empty -- Teams membership sync broken[/red]")
        record_result("FAIL", 9, "Teams Membership", "Teams table is empty")
        suggest("Check ingestion/teams_poller.py and Team.ReadBasic.All permission.")
        return

    rows = await session.execute(
        text("""
            SELECT d.name, d.source, d.confidence,
                   (SELECT COUNT(*) FROM people p WHERE p.department_id = d.id) as member_count
            FROM departments d ORDER BY member_count DESC
        """)
    )
    depts = rows.fetchall()

    if depts:
        console.print()
        dept_table = Table(show_header=True, header_style="bold")
        dept_table.add_column("Department")
        dept_table.add_column("Source")
        dept_table.add_column("Confidence", justify="right")
        dept_table.add_column("Members", justify="right")
        for d in depts:
            dept_table.add_row(d.name, d.source or "?",
                               f"{d.confidence:.2f}" if d.confidence else "?", str(d.member_count))
        console.print(dept_table)

    has_dept_with_members = any(d.member_count > 0 for d in depts) if depts else False

    if teams_count > 0 and channels_count > 0 and members_count > 0 and has_dept_with_members:
        record_result("PASS", 9, "Teams Membership",
                       f"{teams_count} teams, {len(depts)} depts, {members_count} memberships")
    elif teams_count > 0 and not has_dept_with_members:
        record_result("WARNING", 9, "Teams Membership",
                       "Teams exist but no departments with members (org inference may not have run)")
        suggest("Run org_inference manually or wait for the weekly batch job.")
    else:
        record_result("PASS", 9, "Teams Membership", f"{teams_count} teams, {channels_count} channels")


# ── SECTION 10: PEOPLE TABLE HEALTH ─────────────────────────────

async def check_people_health(session) -> None:
    section_header(10, "PEOPLE TABLE HEALTH")

    row = await session.execute(text("SELECT COUNT(*) FROM people"))
    total = row.scalar()

    if total == 0:
        console.print("  [red]People table is empty[/red]")
        record_result("FAIL", 10, "People Health", "People table is empty")
        suggest("Extraction/resolution not creating people. Check resolver.py.")
        return

    rows = await session.execute(
        text("""
            SELECT source, COUNT(*) as cnt,
                   COUNT(*) FILTER (WHERE needs_review = true) as needs_review,
                   COUNT(*) FILTER (WHERE is_external = true) as external
            FROM people GROUP BY source ORDER BY cnt DESC
        """)
    )
    sources = rows.fetchall()
    source_names = {s.source for s in sources}

    table = Table(show_header=True, header_style="bold")
    table.add_column("Source")
    table.add_column("Count", justify="right")
    table.add_column("Needs Review", justify="right")
    table.add_column("External", justify="right")
    for s in sources:
        table.add_row(s.source or "NULL", str(s.cnt), str(s.needs_review), str(s.external))
    console.print(table)

    row2 = await session.execute(
        text("""
            SELECT COUNT(*) FILTER (WHERE department_id IS NOT NULL) as has_dept,
                   COUNT(*) as total
            FROM people
        """)
    )
    r2 = row2.first()
    dept_pct = r2.has_dept / r2.total * 100 if r2.total else 0
    console.print(f"\n  With department: {r2.has_dept} / {r2.total} ({dept_pct:.0f}%)")

    dup_rows = await session.execute(
        text("""
            SELECT name, COUNT(*) as record_count, array_agg(email) as emails
            FROM people GROUP BY name HAVING COUNT(*) > 1
        """)
    )
    dups = dup_rows.fetchall()

    if dups:
        console.print(f"\n  [yellow]Potential duplicates: {len(dups)}[/yellow]")
        if VERBOSE:
            for d in dups[:10]:
                console.print(f"    [dim]- {d.name} ({d.record_count} records): {d.emails}[/dim]")

    multi_source = len(source_names - {None}) >= 3
    few_dups = len(dups) < 5

    if multi_source and dept_pct > 30 and few_dups:
        record_result("PASS", 10, "People Health",
                       f"{total} people from {len(source_names)} sources, {dept_pct:.0f}% with dept, {len(dups)} dups")
    elif len(dups) > 10:
        record_result("WARNING", 10, "People Health", f"{len(dups)} duplicate name groups detected")
        suggest("Review resolver.py entity resolution. May need tighter fuzzy matching.")
    elif not multi_source:
        record_result("WARNING", 10, "People Health",
                       f"People only from sources: {source_names}")
        suggest("Email/Teams extraction should create people from 'email' and 'teams' sources.")
    elif dept_pct <= 30:
        record_result("WARNING", 10, "People Health", f"Only {dept_pct:.0f}% have department assignments")
        suggest("Org inference may need more time or manual department assignments needed.")
    else:
        record_result("PASS", 10, "People Health", f"{total} people")


# ── SECTION 11: WORKSTREAM AUTO-DETECTION ───────────────────────

async def check_workstream_detection(session) -> None:
    section_header(11, "WORKSTREAM AUTO-DETECTION")

    rows = await session.execute(
        text("SELECT created_by, status, COUNT(*) as cnt FROM workstreams GROUP BY created_by, status")
    )
    ws_types = rows.fetchall()

    if not ws_types:
        console.print("  [red]No workstreams exist[/red]")
        record_result("FAIL", 11, "Workstream Detection", "No workstreams exist")
        suggest("Check processing/workstream_detector.py -- detector may not be running.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Created By")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for w in ws_types:
        table.add_row(w.created_by or "NULL", w.status or "NULL", str(w.cnt))
    console.print(table)

    auto_rows = await session.execute(
        text("""
            SELECT w.name, w.confidence, w.status,
                   (SELECT COUNT(*) FROM workstream_items wi WHERE wi.workstream_id = w.id) as items,
                   (SELECT COUNT(DISTINCT person_id) FROM workstream_stakeholders ws
                    WHERE ws.workstream_id = w.id) as stakeholders
            FROM workstreams w WHERE created_by = 'auto'
            ORDER BY items DESC
        """)
    )
    auto_ws = auto_rows.fetchall()

    if auto_ws:
        console.print()
        console.print("  [bold]Auto-detected workstreams:[/bold]")
        ws_table = Table(show_header=True, header_style="bold")
        ws_table.add_column("Name", max_width=35)
        ws_table.add_column("Confidence", justify="right")
        ws_table.add_column("Status")
        ws_table.add_column("Items", justify="right")
        ws_table.add_column("Stakeholders", justify="right")
        for w in auto_ws:
            ws_table.add_row(w.name, f"{w.confidence:.2f}" if w.confidence else "?",
                             w.status, str(w.items), str(w.stakeholders))
        console.print(ws_table)

    row_multi = await session.execute(
        text("""
            SELECT COUNT(*) FROM (
                SELECT item_type, item_id FROM workstream_items
                GROUP BY item_type, item_id HAVING COUNT(*) > 1
            ) multi
        """)
    )
    multi_count = row_multi.scalar()
    console.print(f"\n  Items in multiple workstreams: [bold]{multi_count}[/bold]")

    unassigned_rows = await session.execute(
        text("""
            SELECT 'emails' as type, COUNT(*) FROM emails e
            WHERE email_class = 'human' AND triage_class IN ('substantive','contextual')
              AND NOT EXISTS (SELECT 1 FROM workstream_items wi WHERE wi.item_type = 'email' AND wi.item_id = e.id)
            UNION ALL
            SELECT 'chat_messages', COUNT(*) FROM chat_messages cm
            WHERE noise_filtered = false AND triage_class IN ('substantive','contextual')
              AND NOT EXISTS (SELECT 1 FROM workstream_items wi WHERE wi.item_type = 'chat_message' AND wi.item_id = cm.id)
            UNION ALL
            SELECT 'meetings', COUNT(*) FROM meetings m
            WHERE is_excluded = false AND processing_status = 'completed'
              AND NOT EXISTS (SELECT 1 FROM workstream_items wi WHERE wi.item_type = 'meeting' AND wi.item_id = m.id)
        """)
    )
    unassigned = unassigned_rows.fetchall()
    console.print("\n  Unassigned items:")
    for u in unassigned:
        console.print(f"    {u[0]:20s} {u[1]}")

    has_3plus_auto = len(auto_ws) >= 3
    has_5plus_items = any(w.items >= 5 for w in auto_ws) if auto_ws else False

    if has_3plus_auto and has_5plus_items:
        record_result("PASS", 11, "Workstream Detection",
                       f"{len(auto_ws)} auto workstreams, multi-ws items: {multi_count}")
    elif len(auto_ws) >= 1:
        record_result("WARNING", 11, "Workstream Detection",
                       f"Only {len(auto_ws)} auto workstreams (expected 3+) or all <5 items")
        suggest("Workstream detector may need more data or lower thresholds.")
    else:
        record_result("FAIL", 11, "Workstream Detection",
                       "Zero auto-detected workstreams -- FAIL since system has had email+Teams data")
        suggest("Check processing/workstream_detector.py and ensure the weekly batch is scheduled.")

    if VERBOSE and auto_ws:
        console.print()
        for w in auto_ws[:3]:
            console.print(f"  [dim]Workstream: {w.name}[/dim]")
            sample_items = await session.execute(
                text("""
                    SELECT wi.item_type, wi.item_id,
                           CASE
                               WHEN wi.item_type = 'email' THEN (SELECT subject FROM emails WHERE id = wi.item_id)
                               WHEN wi.item_type = 'meeting' THEN (SELECT title FROM meetings WHERE id = wi.item_id)
                               WHEN wi.item_type = 'chat_message' THEN (SELECT body_preview FROM chat_messages WHERE id = wi.item_id)
                               ELSE NULL
                           END as preview
                    FROM workstream_items wi
                    WHERE wi.workstream_id = (SELECT id FROM workstreams WHERE name = :name LIMIT 1)
                    LIMIT 5
                """),
                {"name": w.name},
            )
            for item in sample_items.fetchall():
                console.print(f"    [dim]- [{item.item_type}] {(item.preview or '(no preview)')[:60]}[/dim]")


# ── SECTION 12: EMBEDDINGS ──────────────────────────────────────

async def check_embeddings(session) -> None:
    section_header(12, "EMBEDDINGS & VECTOR SEARCH")

    rows = await session.execute(
        text("""
            SELECT 'meetings' as type,
                   COUNT(*) FILTER (WHERE embedding IS NOT NULL) as has_embedding,
                   COUNT(*) as total
            FROM meetings WHERE processing_status = 'completed'
            UNION ALL
            SELECT 'emails',
                   COUNT(*) FILTER (WHERE embedding IS NOT NULL),
                   COUNT(*)
            FROM emails WHERE triage_class IN ('substantive','contextual')
            UNION ALL
            SELECT 'chat_messages',
                   COUNT(*) FILTER (WHERE embedding IS NOT NULL),
                   COUNT(*)
            FROM chat_messages WHERE triage_class IN ('substantive','contextual')
        """)
    )
    embeddings = rows.fetchall()

    total_items = 0
    total_with = 0

    table = Table(show_header=True, header_style="bold")
    table.add_column("Type")
    table.add_column("Has Embedding", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Coverage", justify="right")

    for e in embeddings:
        pct = e.has_embedding / e.total * 100 if e.total else 0
        table.add_row(e.type, str(e.has_embedding), str(e.total), f"{pct:.0f}%" if e.total else "N/A")
        total_items += e.total
        total_with += e.has_embedding

    console.print(table)

    overall_pct = total_with / total_items * 100 if total_items else 0
    console.print(f"\n  Overall coverage: [bold]{overall_pct:.0f}%[/bold]")

    if overall_pct >= 90:
        record_result("PASS", 12, "Embeddings", f"{overall_pct:.0f}% coverage")
    elif overall_pct >= 70:
        record_result("WARNING", 12, "Embeddings", f"{overall_pct:.0f}% coverage (target >90%)")
        suggest("Check processing/embeddings.py -- some items may have failed embedding generation.")
    elif total_items == 0:
        record_result("WARNING", 12, "Embeddings", "No items to embed yet")
    else:
        record_result("FAIL", 12, "Embeddings", f"Only {overall_pct:.0f}% embedding coverage (target >90%)")
        suggest("Embedding generation may be broken. Check processing/embeddings.py and OpenAI API key.")


# ── SECTION 13: LLM COST (cumulative) ──────────────────────────

async def check_llm_costs(session) -> None:
    section_header(13, "LLM COST TRACKING")

    rows = await session.execute(
        text("""
            SELECT model, task,
                   SUM(input_tokens) as input_tok,
                   SUM(output_tokens) as output_tok,
                   SUM(calls) as total_calls
            FROM llm_usage WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY model, task ORDER BY total_calls DESC
        """)
    )
    usage = rows.fetchall()

    if not usage:
        console.print("  [red]llm_usage table is empty (cost tracking not implemented)[/red]")
        record_result("FAIL", 13, "LLM Costs", "llm_usage table is empty")
        suggest("Ensure all LLM calls upsert into llm_usage table after each call.")
        return

    PRICING = {
        "haiku": (0.25 / 1_000_000, 1.25 / 1_000_000),
        "sonnet": (3.0 / 1_000_000, 15.0 / 1_000_000),
        "embedding": (0.02 / 1_000_000, 0.0),
    }

    table = Table(show_header=True, header_style="bold")
    table.add_column("Model")
    table.add_column("Task")
    table.add_column("Calls", justify="right")
    table.add_column("Input Tok", justify="right")
    table.add_column("Output Tok", justify="right")
    table.add_column("Est. Cost", justify="right")

    total_cost = 0.0
    for u in usage:
        model_lower = (u.model or "").lower()
        if "haiku" in model_lower:
            price = PRICING["haiku"]
        elif "sonnet" in model_lower:
            price = PRICING["sonnet"]
        elif "embed" in model_lower:
            price = PRICING["embedding"]
        else:
            price = PRICING["haiku"]

        cost = (u.input_tok or 0) * price[0] + (u.output_tok or 0) * price[1]
        total_cost += cost

        table.add_row(
            u.model, u.task, str(u.total_calls),
            f"{u.input_tok:,}" if u.input_tok else "0",
            f"{u.output_tok:,}" if u.output_tok else "0",
            f"${cost:.4f}",
        )

    console.print(table)
    console.print(f"\n  Estimated 7-day cost: [bold]${total_cost:.2f}[/bold]")

    if total_cost < 20:
        record_result("PASS", 13, "LLM Costs", f"${total_cost:.2f}/week")
    elif total_cost <= 35:
        record_result("WARNING", 13, "LLM Costs", f"${total_cost:.2f}/week (higher than expected)")
        suggest("Check if noise filter/triage is working -- may be over-extracting.")
    else:
        record_result("FAIL", 13, "LLM Costs", f"${total_cost:.2f}/week (cost is too high)")
        suggest("Urgent: check triage thresholds and noise filter.")


# ── SECTION 14: CROSS-SYSTEM INTEGRATION ────────────────────────

async def check_integration(session) -> None:
    section_header(14, "CROSS-SYSTEM INTEGRATION")

    checks = []

    # Email -> People
    row = await session.execute(
        text("""
            SELECT COUNT(*) FILTER (WHERE sender_id IS NOT NULL) as resolved,
                   COUNT(*) as total
            FROM emails WHERE email_class = 'human'
        """)
    )
    r = row.first()
    email_resolved_pct = r.resolved / r.total * 100 if r.total else 0
    console.print(f"  Email -> People resolution: {r.resolved}/{r.total} ({email_resolved_pct:.0f}%)")
    if email_resolved_pct >= 80:
        checks.append(("PASS", "Email->People", f"{email_resolved_pct:.0f}% resolved"))
    elif email_resolved_pct >= 50:
        checks.append(("WARNING", "Email->People", f"Only {email_resolved_pct:.0f}% resolved"))
    elif r.total > 0:
        checks.append(("FAIL", "Email->People", f"Only {email_resolved_pct:.0f}% resolved"))
        suggest("Entity resolution not running on email senders. Check resolver.py.")
    else:
        checks.append(("WARNING", "Email->People", "No human emails to check"))

    # Chat -> People
    row2 = await session.execute(
        text("""
            SELECT COUNT(*) FILTER (WHERE sender_id IS NOT NULL) as resolved,
                   COUNT(*) as total
            FROM chat_messages WHERE noise_filtered = false
        """)
    )
    r2 = row2.first()
    chat_resolved_pct = r2.resolved / r2.total * 100 if r2.total else 0
    console.print(f"  Chat -> People resolution:  {r2.resolved}/{r2.total} ({chat_resolved_pct:.0f}%)")
    if chat_resolved_pct >= 80:
        checks.append(("PASS", "Chat->People", f"{chat_resolved_pct:.0f}% resolved"))
    elif chat_resolved_pct >= 50:
        checks.append(("WARNING", "Chat->People", f"Only {chat_resolved_pct:.0f}% resolved"))
    elif r2.total > 0:
        checks.append(("FAIL", "Chat->People", f"Only {chat_resolved_pct:.0f}% resolved"))
        suggest("Entity resolution not running on chat senders. Check resolver.py.")
    else:
        checks.append(("WARNING", "Chat->People", "No non-filtered chats to check"))

    # Email asks linked to action items
    row3 = await session.execute(
        text("SELECT COUNT(*) FROM email_asks WHERE linked_action_item_id IS NOT NULL")
    )
    linked_asks = row3.scalar()
    console.print(f"  Email asks linked to action items: {linked_asks}")
    if linked_asks and linked_asks > 0:
        checks.append(("PASS", "Ask->Action linking", f"{linked_asks} links"))
    else:
        checks.append(("WARNING", "Ask->Action linking", "Zero links"))

    # Meeting chat correlation
    row4 = await session.execute(
        text("SELECT COUNT(*) FROM chat_messages WHERE linked_meeting_id IS NOT NULL")
    )
    meeting_chats = row4.scalar()
    console.print(f"  Meeting chat links: {meeting_chats}")
    if meeting_chats and meeting_chats > 0:
        checks.append(("PASS", "Meeting chat correlation", f"{meeting_chats} linked"))
    else:
        checks.append(("WARNING", "Meeting chat correlation", "Zero links"))

    # Overall status
    has_fail = any(c[0] == "FAIL" for c in checks)
    has_warn = any(c[0] == "WARNING" for c in checks)

    for icon_str, label, detail in checks:
        icon = {"PASS": "[green]OK[/green]", "WARNING": "[yellow]!![/yellow]", "FAIL": "[red]XX[/red]"}[icon_str]
        console.print(f"  {icon} {label}: {detail}")

    if has_fail:
        record_result("FAIL", 14, "Cross-System Integration",
                       "; ".join(f"{c[1]}: {c[2]}" for c in checks if c[0] == "FAIL"))
    elif has_warn:
        record_result("WARNING", 14, "Cross-System Integration",
                       "; ".join(f"{c[1]}: {c[2]}" for c in checks if c[0] == "WARNING"))
    else:
        record_result("PASS", 14, "Cross-System Integration", "All integration checks passed")


# ══════════════════════════════════════════════════════════════════
#                    PART B: PHASE 4 RE-CHECKS
# ══════════════════════════════════════════════════════════════════

# ── SECTION 15: SCHEDULER HEALTH (Phase 4 services) ─────────────

async def check_scheduler_health(session) -> None:
    section_header(15, "SCHEDULER HEALTH (Phase 4 services)")

    rows = await session.execute(
        text("""
            SELECT service, status, last_success, last_error, last_error_message,
                   items_processed_last_hour
            FROM system_health
            WHERE service IN (
                'morning_briefing', 'monday_brief', 'friday_recap',
                'meeting_prep', 'draft_generator', 'sentiment_aggregator',
                'readiness_scorer', 'dashboard_cache'
            )
            ORDER BY service
        """)
    )
    services = rows.fetchall()

    if not services:
        console.print("  [red]Zero intelligence services in system_health[/red]")
        record_result("FAIL", 15, "Scheduler Health", "Zero intelligence services registered (scheduler not wired up)")
        suggest("Ensure intelligence/scheduler.py registers jobs and updates system_health.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Status", width=4)
    table.add_column("Service", min_width=22)
    table.add_column("State", min_width=10)
    table.add_column("Last Success", min_width=16)
    table.add_column("Items/hr", justify="right", min_width=8)

    now = datetime.now(timezone.utc)
    recent_success_count = 0
    for svc in services:
        icon = {"healthy": "[green]OK[/green]", "degraded": "[yellow]!![/yellow]", "down": "[red]XX[/red]"}.get(
            svc.status, "[dim]??[/dim]"
        )
        ls = svc.last_success
        if ls:
            if ls.tzinfo is None:
                from zoneinfo import ZoneInfo
                ls = ls.replace(tzinfo=ZoneInfo("UTC"))
            if (now - ls).total_seconds() < 86400:
                recent_success_count += 1

        table.add_row(
            icon, svc.service, svc.status or "unknown",
            time_ago(svc.last_success),
            str(svc.items_processed_last_hour or 0),
        )

    console.print(table)

    if recent_success_count >= 3:
        record_result("PASS", 15, "Scheduler Health",
                       f"{recent_success_count}/{len(services)} services succeeded in last 24h")
    elif recent_success_count > 0:
        record_result("WARNING", 15, "Scheduler Health",
                       f"Only {recent_success_count}/{len(services)} services succeeded in last 24h")
    else:
        all_null = all(svc.last_success is None for svc in services)
        if all_null:
            record_result("WARNING", 15, "Scheduler Health",
                           "Services registered but last_success is NULL (never ran)")
        else:
            record_result("WARNING", 15, "Scheduler Health",
                           "No services succeeded in last 24h")
        suggest("Check if scheduled times have passed. Verify APScheduler is running.")


# ── SECTION 16: BRIEFINGS GENERATED ─────────────────────────────

async def check_briefings(session) -> None:
    section_header(16, "BRIEFINGS GENERATED")

    rows = await session.execute(
        text("""
            SELECT briefing_type, COUNT(*) as cnt,
                   MAX(generated_at) as most_recent,
                   MIN(generated_at) as earliest
            FROM briefings
            GROUP BY briefing_type
        """)
    )
    by_type = rows.fetchall()

    if not by_type:
        console.print("  [red]Briefings table is empty[/red]")
        record_result("FAIL", 16, "Briefings", "briefings table is empty")
        suggest("Check intelligence/briefings.py and scheduler.py.")
        return

    type_dict = {r.briefing_type: r for r in by_type}

    table = Table(show_header=True, header_style="bold")
    table.add_column("Type")
    table.add_column("Count", justify="right")
    table.add_column("Most Recent")
    table.add_column("Earliest")

    for r in by_type:
        table.add_row(r.briefing_type, str(r.cnt), time_ago(r.most_recent), time_ago(r.earliest))

    console.print(table)

    for btype in ["morning", "monday", "friday", "meeting_prep"]:
        present = btype in type_dict
        icon = "[green]Y[/green]" if present else "[red]N[/red]"
        console.print(f"  {icon} {btype} briefing exists: {'yes' if present else 'no'}")

    detail_rows = await session.execute(
        text("""
            SELECT briefing_type, generated_at,
                   LENGTH(content) as content_length,
                   LEFT(content, 200) as preview
            FROM briefings b1
            WHERE generated_at = (
                SELECT MAX(generated_at) FROM briefings b2
                WHERE b2.briefing_type = b1.briefing_type
            )
            ORDER BY briefing_type
        """)
    )
    details = detail_rows.fetchall()

    if VERBOSE:
        console.print()
        for d in details:
            console.print(f"  [dim]{d.briefing_type} ({d.content_length} chars): {d.preview}...[/dim]")

    has_morning = "morning" in type_dict
    morning_length = 0
    for d in details:
        if d.briefing_type == "morning":
            morning_length = d.content_length or 0

    if has_morning and morning_length > 500:
        record_result("PASS", 16, "Briefings",
                       f"{len(type_dict)} types, morning={morning_length} chars")
    elif has_morning and morning_length > 0:
        record_result("WARNING", 16, "Briefings",
                       f"Morning briefing exists but short ({morning_length} chars)")
    elif by_type:
        record_result("WARNING", 16, "Briefings",
                       f"Briefings exist ({[r.briefing_type for r in by_type]}) but no morning type")
    else:
        record_result("FAIL", 16, "Briefings", "No briefings generated")


# ── SECTION 17: MEETING PREP PRE-GENERATION ─────────────────────

async def check_meeting_prep_timing(session) -> None:
    section_header(17, "MEETING PREP PRE-GENERATION")

    rows = await session.execute(
        text("""
            SELECT m.title, m.start_time, b.generated_at,
                   CASE WHEN b.generated_at < m.start_time THEN true
                        ELSE false END as pre_generated
            FROM briefings b
            JOIN meetings m ON b.related_meeting_id = m.id
            WHERE b.briefing_type = 'meeting_prep'
            ORDER BY m.start_time DESC LIMIT 10
        """)
    )
    preps = rows.fetchall()

    if not preps:
        console.print("  [yellow]No meeting prep briefs linked to meetings[/yellow]")
        record_result("WARNING", 17, "Meeting Prep Timing", "No prep briefs linked to meetings to evaluate timing")
        suggest("Check intelligence/meeting_prep.py and ensure related_meeting_id is set.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Meeting", max_width=35)
    table.add_column("Start Time")
    table.add_column("Generated At")
    table.add_column("Timing")

    pre_count = 0
    for p in preps:
        timing = "pre-generated" if p.pre_generated else "generated late"
        style = "[green]" if p.pre_generated else "[yellow]"
        if p.pre_generated:
            pre_count += 1
        table.add_row(
            (p.title or "")[:35],
            str(p.start_time)[:19] if p.start_time else "?",
            str(p.generated_at)[:19] if p.generated_at else "?",
            f"{style}{timing}[/{style[1:]}",
        )

    console.print(table)

    pct = pre_count / len(preps) * 100 if preps else 0
    console.print(f"\n  Pre-generated: {pre_count}/{len(preps)} ({pct:.0f}%)")

    if pct >= 80:
        record_result("PASS", 17, "Meeting Prep Timing", f"{pct:.0f}% pre-generated")
    elif pct >= 50:
        record_result("WARNING", 17, "Meeting Prep Timing", f"Only {pct:.0f}% pre-generated")
    else:
        record_result("FAIL", 17, "Meeting Prep Timing", f"Only {pct:.0f}% pre-generated (most generated late)")
        suggest("Meeting prep should be pre-computed with the daily briefing, not generated on-demand.")


# ── SECTION 18: MORNING BRIEFING CONTENT VALIDATION ─────────────

async def check_morning_content(session) -> None:
    section_header(18, "MORNING BRIEFING CONTENT VALIDATION")

    row = await session.execute(
        text("""
            SELECT content, generated_at, LENGTH(content) as content_length
            FROM briefings
            WHERE briefing_type = 'morning'
            ORDER BY generated_at DESC LIMIT 1
        """)
    )
    latest = row.first()

    if not latest:
        console.print("  [red]No morning briefing found[/red]")
        record_result("FAIL", 18, "Morning Content", "No morning briefing exists")
        return

    content = latest.content.lower()
    console.print(f"  Latest morning briefing: {latest.content_length} chars, generated {time_ago(latest.generated_at)}")

    checks = {
        "calendar/meetings": any(w in content for w in ["meeting", "calendar", "today", "schedule"]),
        "action items": any(w in content for w in ["action", "overdue", "pending", "awaiting"]),
        "workstreams": any(w in content for w in ["workstream", "active", "status"]),
        "topics/agenda": any(w in content for w in ["address", "discuss", "raise", "topic", "agenda"]),
    }

    present_count = sum(1 for v in checks.values() if v)
    for section_name, found in checks.items():
        icon = "[green]Y[/green]" if found else "[red]N[/red]"
        console.print(f"  {icon} Contains {section_name}: {'yes' if found else 'no'}")

    if VERBOSE:
        console.print()
        console.print("  [dim]Briefing preview (first 500 chars):[/dim]")
        console.print(f"  [dim]{latest.content[:500]}[/dim]")

    if present_count >= 4:
        record_result("PASS", 18, "Morning Content", f"All {present_count}/4 expected sections present")
    elif present_count >= 2:
        missing = [k for k, v in checks.items() if not v]
        record_result("WARNING", 18, "Morning Content", f"Missing sections: {', '.join(missing)}")
    else:
        record_result("FAIL", 18, "Morning Content",
                       f"Only {present_count}/4 sections -- content may be generic boilerplate")
        suggest("Check intelligence/briefings.py prompts and data retrieval.")


# ── SECTION 19: VOICE PROFILE ───────────────────────────────────

async def check_voice_profile(session) -> None:
    section_header(19, "VOICE PROFILE")

    row = await session.execute(
        text("""
            SELECT id,
                   LENGTH(auto_profile) as profile_length,
                   LEFT(auto_profile, 300) as profile_preview,
                   array_length(custom_rules, 1) as custom_rule_count,
                   last_learned_at,
                   updated
            FROM voice_profile LIMIT 1
        """)
    )
    vp = row.first()

    if not vp:
        console.print("  [red]voice_profile table is empty[/red]")
        record_result("FAIL", 19, "Voice Profile", "voice_profile table is empty")
        suggest("Phase 0 backfill should have generated an initial profile. Check scripts/backfill.py.")
        return

    console.print(f"  Profile length:    [bold]{vp.profile_length or 0}[/bold] chars")
    console.print(f"  Custom rules:      [bold]{vp.custom_rule_count or 0}[/bold]")
    console.print(f"  Last learned:      {time_ago(vp.last_learned_at)}")
    console.print(f"  Updated:           {time_ago(vp.updated)}")

    if VERBOSE and vp.profile_preview:
        console.print()
        console.print("  [dim]Profile preview:[/dim]")
        console.print(f"  [dim]{vp.profile_preview}[/dim]")

    length = vp.profile_length or 0
    if length > 200:
        record_result("PASS", 19, "Voice Profile", f"Profile exists ({length} chars)")
    elif length > 0:
        record_result("WARNING", 19, "Voice Profile", f"Profile exists but short ({length} chars)")
        suggest("Voice profile may need more sent emails to learn from.")
    else:
        record_result("WARNING", 19, "Voice Profile", "Profile record exists but auto_profile is empty/NULL")
        suggest("Check intelligence/voice_profile.py -- learning may not be running.")


# ── SECTION 20: DRAFT GENERATION ────────────────────────────────

async def check_drafts(session) -> None:
    section_header(20, "DRAFT GENERATION")

    rows = await session.execute(
        text("""
            SELECT draft_type, status, COUNT(*) as cnt
            FROM drafts
            GROUP BY draft_type, status
            ORDER BY draft_type, status
        """)
    )
    draft_data = rows.fetchall()

    if not draft_data:
        console.print("  [red]Drafts table is empty[/red]")

        stale_rows = await session.execute(
            text("""
                SELECT 'action_items' as type, COUNT(*)
                FROM action_items
                WHERE status = 'open'
                  AND updated < NOW() - INTERVAL '7 days'
                UNION ALL
                SELECT 'email_asks', COUNT(*)
                FROM email_asks
                WHERE status = 'open'
                  AND created < NOW() - INTERVAL '72 hours'
                UNION ALL
                SELECT 'chat_asks', COUNT(*)
                FROM chat_asks
                WHERE status = 'open'
                  AND created < NOW() - INTERVAL '72 hours'
            """)
        )
        stale = stale_rows.fetchall()
        total_stale = sum(s[1] for s in stale)

        if total_stale > 0:
            console.print(f"  [yellow]{total_stale} stale items exist but no nudge drafts generated[/yellow]")
            for s in stale:
                console.print(f"    {s[0]:20s} {s[1]} stale")
            record_result("FAIL", 20, "Drafts", f"{total_stale} stale items but zero drafts")
            suggest("Check intelligence/draft_generator.py -- stale item detection or nudge generation broken.")
        else:
            record_result("WARNING", 20, "Drafts", "No drafts and no stale items to trigger them")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Draft Type")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for d in draft_data:
        table.add_row(d.draft_type, d.status, str(d.cnt))
    console.print(table)

    types = {d.draft_type for d in draft_data}
    has_nudge = "nudge" in types

    console.print()
    console.print(f"  Auto-nudges generated: {'[green]yes[/green]' if has_nudge else '[yellow]no[/yellow]'}")
    console.print(f"  Meeting recaps generated: {'[green]yes[/green]' if 'recap' in types else '[yellow]no[/yellow]'}")

    if VERBOSE:
        console.print()
        console.print("  [dim]Recent drafts:[/dim]")
        detail_rows = await session.execute(
            text("""
                SELECT d.draft_type, d.status, d.channel,
                       p.name as recipient,
                       d.subject, LEFT(d.body, 150) as body_preview,
                       d.triggered_by_type, d.created
                FROM drafts d
                LEFT JOIN people p ON d.recipient_id = p.id
                ORDER BY d.created DESC LIMIT 10
            """)
        )
        for d in detail_rows.fetchall():
            console.print(f"    [dim]- [{d.draft_type}/{d.status}] to: {d.recipient or '?'} | "
                          f"{(d.subject or '(no subject)')[:40]}[/dim]")

    if has_nudge:
        record_result("PASS", 20, "Drafts", f"{sum(d.cnt for d in draft_data)} drafts, nudges present")
    elif draft_data:
        record_result("WARNING", 20, "Drafts",
                       "Drafts exist but no auto-nudges (only user-triggered)")
        suggest("Check intelligence/draft_generator.py stale item detection.")
    else:
        record_result("FAIL", 20, "Drafts", "No drafts generated")


# ── SECTION 21: RESPONSE WORKFLOW ────────────────────────────────

async def check_response_workflow(session) -> None:
    section_header(21, "RESPONSE WORKFLOW INFRASTRUCTURE")

    thread_rows = await session.execute(
        text("""
            SELECT COUNT(*) FILTER (WHERE status = 'sent') as sent,
                   COUNT(*) FILTER (WHERE status = 'pending_review') as pending,
                   COUNT(*) FILTER (WHERE conversation_id IS NOT NULL) as has_email_thread,
                   COUNT(*) FILTER (WHERE chat_id IS NOT NULL) as has_chat_thread,
                   COUNT(*) as total
            FROM drafts
        """)
    )
    tr = thread_rows.first()

    if tr.total == 0:
        console.print("  [red]No drafts at all -- nothing to test workflow with[/red]")
        record_result("FAIL", 21, "Response Workflow", "No drafts exist")
        return

    console.print(f"  Total drafts: [bold]{tr.total}[/bold]")
    console.print(f"    Sent: {tr.sent}")
    console.print(f"    Pending review: {tr.pending}")
    console.print(f"    With email conversation_id: {tr.has_email_thread}")
    console.print(f"    With chat_id: {tr.has_chat_thread}")

    if tr.sent and tr.sent > 0:
        record_result("PASS", 21, "Response Workflow",
                       f"{tr.sent} drafts sent, threading: email={tr.has_email_thread}, chat={tr.has_chat_thread}")
    elif tr.has_email_thread > 0 or tr.has_chat_thread > 0:
        record_result("PASS", 21, "Response Workflow",
                       "Drafts have threading data (none sent yet -- OK, user hasn't clicked Send)")
    elif tr.total > 0 and tr.has_email_thread == 0 and tr.has_chat_thread == 0:
        record_result("WARNING", 21, "Response Workflow",
                       "All drafts missing threading data (sends may not thread correctly)")
        suggest("Check draft_generator.py -- conversation_id/chat_id should be set from source item.")
    else:
        record_result("WARNING", 21, "Response Workflow", "Drafts exist but no sends yet")


# ── SECTION 22: READINESS SCORES ────────────────────────────────

async def check_readiness(session) -> None:
    section_header(22, "READINESS SCORES")

    row = await session.execute(
        text("""
            SELECT key, data, computed_at,
                   LENGTH(data::text) as data_size
            FROM dashboard_cache
            WHERE key = 'readiness_scores'
        """)
    )
    cache_row = row.first()

    if not cache_row:
        console.print("  [red]No readiness_scores in dashboard_cache[/red]")
        record_result("FAIL", 22, "Readiness Scores", "readiness_scores not in cache")
        suggest("Check intelligence/readiness.py and ensure the scorer is running as a scheduled job.")
        return

    console.print(f"  Cache entry found: {cache_row.data_size} bytes, computed {time_ago(cache_row.computed_at)}")

    try:
        data = cache_row.data if isinstance(cache_row.data, (list, dict)) else json.loads(cache_row.data)
        if isinstance(data, list):
            scores = data
        elif isinstance(data, dict) and "scores" in data:
            scores = data["scores"]
        elif isinstance(data, dict):
            scores = list(data.values()) if data else []
        else:
            scores = []
    except (json.JSONDecodeError, TypeError):
        console.print("  [yellow]Could not parse readiness_scores JSONB data[/yellow]")
        record_result("WARNING", 22, "Readiness Scores", "Data exists but could not parse JSONB")
        return

    if not scores:
        console.print("  [yellow]readiness_scores cache is empty list/dict[/yellow]")
        record_result("WARNING", 22, "Readiness Scores", "Cache exists but contains no scores")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Person", max_width=25)
    table.add_column("Score", justify="right")
    table.add_column("Open Items", justify="right")
    table.add_column("Blocking", justify="right")
    table.add_column("Trend")

    score_values = []
    for s in scores[:15]:
        if isinstance(s, dict):
            name = s.get("name", s.get("person_name", "?"))
            score = s.get("score", 0)
            open_items = s.get("open_items", "?")
            blocking = s.get("blocking_count", s.get("blocking", "?"))
            trend = s.get("trend", "?")
            trend_icon = {"up": "^", "down": "v", "flat": "-"}.get(str(trend), str(trend))
            score_values.append(score)
            table.add_row(str(name)[:25], str(score), str(open_items), str(blocking), trend_icon)

    console.print(table)

    unique_scores = set(score_values)

    if len(scores) >= 3 and all(0 <= s <= 100 for s in score_values if isinstance(s, (int, float))):
        if len(unique_scores) == 1 and len(score_values) > 1:
            record_result("WARNING", 22, "Readiness Scores",
                           f"{len(scores)} people but all scores identical ({score_values[0]}) -- normalization may be broken")
        else:
            record_result("PASS", 22, "Readiness Scores",
                           f"{len(scores)} people scored, range {min(score_values)}-{max(score_values)}")
    elif len(scores) > 0:
        record_result("WARNING", 22, "Readiness Scores",
                       f"Only {len(scores)} people scored (expected 3+)")
    else:
        record_result("FAIL", 22, "Readiness Scores", "No scores in cache data")


# ── SECTION 23: SENTIMENT AGGREGATION ───────────────────────────

async def check_sentiment(session) -> None:
    section_header(23, "SENTIMENT AGGREGATION")

    rows = await session.execute(
        text("""
            SELECT scope_type, COUNT(*) as cnt,
                   ROUND(AVG(avg_score)::numeric, 1) as mean_sentiment,
                   MIN(period_start) as earliest,
                   MAX(period_end) as latest
            FROM sentiment_aggregations
            GROUP BY scope_type
        """)
    )
    by_scope = rows.fetchall()

    if not by_scope:
        console.print("  [red]sentiment_aggregations table is empty[/red]")
        record_result("FAIL", 23, "Sentiment", "sentiment_aggregations table is empty")
        suggest("Check intelligence/sentiment.py -- aggregation job may not be running.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Scope Type")
    table.add_column("Count", justify="right")
    table.add_column("Mean Score", justify="right")
    table.add_column("Earliest")
    table.add_column("Latest")

    scope_types = set()
    for r in by_scope:
        scope_types.add(r.scope_type)
        table.add_row(r.scope_type, str(r.cnt), str(r.mean_sentiment),
                       str(r.earliest) if r.earliest else "?",
                       str(r.latest) if r.latest else "?")

    console.print(table)

    friction_rows = await session.execute(
        text("""
            SELECT scope_id, avg_score, trend, interaction_count
            FROM sentiment_aggregations
            WHERE scope_type = 'cross_department'
              AND avg_score < 65
            ORDER BY avg_score ASC LIMIT 10
        """)
    )
    friction = friction_rows.fetchall()

    if friction:
        console.print(f"\n  [yellow]Cross-department friction detected: {len(friction)} pairs[/yellow]")
        if VERBOSE:
            for f in friction:
                console.print(f"    [dim]- {f.scope_id}: score={f.avg_score}, "
                              f"trend={f.trend}, interactions={f.interaction_count}[/dim]")
    else:
        console.print("\n  [dim]No cross-department friction detected (may be normal)[/dim]")

    if len(scope_types) >= 2:
        record_result("PASS", 23, "Sentiment", f"{len(scope_types)} scope types, friction pairs: {len(friction)}")
    elif len(scope_types) == 1:
        record_result("WARNING", 23, "Sentiment",
                       f"Only 1 scope type ({list(scope_types)[0]}) -- expected 2+")
        suggest("Sentiment aggregator may not be computing all scope types.")
    else:
        record_result("FAIL", 23, "Sentiment", "No sentiment data")


# ── SECTION 24: RAG CHAT INFRASTRUCTURE ─────────────────────────

async def check_rag_chat(session) -> None:
    section_header(24, "RAG CHAT INFRASTRUCTURE")

    try:
        rows = await session.execute(
            text("""
                SELECT COUNT(*) as total_sessions,
                       COUNT(*) FILTER (WHERE last_active > NOW() - INTERVAL '24 hours') as recent_sessions,
                       MAX(jsonb_array_length(messages)) as max_messages_in_session
                FROM chat_sessions
            """)
        )
        r = rows.first()
        console.print(f"  Chat sessions: [bold]{r.total_sessions}[/bold]")
        console.print(f"  Recent (24h): [bold]{r.recent_sessions}[/bold]")
        console.print(f"  Max messages in session: [bold]{r.max_messages_in_session or 0}[/bold]")
    except Exception as ex:
        console.print(f"  [red]chat_sessions query failed: {ex}[/red]")
        record_result("FAIL", 24, "RAG Chat", f"chat_sessions table error: {ex}")
        return

    embed_rows = await session.execute(
        text("""
            SELECT COUNT(*) as cnt FROM (
                SELECT 1 FROM meetings WHERE embedding IS NOT NULL
                UNION ALL
                SELECT 1 FROM emails WHERE embedding IS NOT NULL
                UNION ALL
                SELECT 1 FROM chat_messages WHERE embedding IS NOT NULL
            ) all_embeddings
        """)
    )
    searchable = embed_rows.scalar()
    console.print(f"  Searchable items with embeddings: [bold]{searchable}[/bold]")

    if searchable > 100:
        record_result("PASS", 24, "RAG Chat", f"Infrastructure OK, {searchable} searchable items")
    elif searchable >= 10:
        record_result("WARNING", 24, "RAG Chat",
                       f"Only {searchable} searchable items (search results may be limited)")
    else:
        record_result("FAIL", 24, "RAG Chat", f"Only {searchable} searchable items (need >10 for meaningful RAG)")
        suggest("Embedding coverage is too low for RAG. Fix embeddings first (Section 12).")


# ── SECTION 25: DASHBOARD CACHE ─────────────────────────────────

async def check_dashboard_cache(session) -> None:
    section_header(25, "DASHBOARD CACHE")

    expected_keys = {
        'workstream_cards', 'pending_decisions', 'awaiting_response',
        'stale_items', 'todays_meetings', 'drafts_pending',
        'readiness_scores', 'department_health',
    }

    rows = await session.execute(
        text("""
            SELECT key, computed_at,
                   EXTRACT(EPOCH FROM (NOW() - computed_at)) / 60 as minutes_stale,
                   LENGTH(data::text) as data_size_bytes
            FROM dashboard_cache
            ORDER BY key
        """)
    )
    cache_entries = rows.fetchall()

    if not cache_entries:
        console.print("  [red]dashboard_cache is empty[/red]")
        record_result("FAIL", 25, "Dashboard Cache", "dashboard_cache table is empty")
        suggest("Check dashboard cache refresh job in scheduler.py.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Key")
    table.add_column("Data Size", justify="right")
    table.add_column("Computed")
    table.add_column("Minutes Stale", justify="right")

    found_keys = set()
    stale_count = 0
    for c in cache_entries:
        found_keys.add(c.key)
        mins = c.minutes_stale or 0
        stale_marker = " [yellow]STALE[/yellow]" if mins > 30 else ""
        if mins > 30:
            stale_count += 1
        table.add_row(
            c.key,
            f"{c.data_size_bytes:,}" if c.data_size_bytes else "0",
            time_ago(c.computed_at),
            f"{mins:.0f}{stale_marker}",
        )

    console.print(table)

    present = found_keys & expected_keys
    missing = expected_keys - found_keys
    extra = found_keys - expected_keys

    console.print(f"\n  Expected keys present: {len(present)}/{len(expected_keys)}")
    if missing:
        console.print(f"  [yellow]Missing: {', '.join(sorted(missing))}[/yellow]")
    if extra:
        console.print(f"  [dim]Extra keys: {', '.join(sorted(extra))}[/dim]")

    if len(present) >= 6 and stale_count == 0:
        record_result("PASS", 25, "Dashboard Cache",
                       f"{len(present)}/{len(expected_keys)} keys, all fresh")
    elif len(present) >= 6:
        record_result("WARNING", 25, "Dashboard Cache",
                       f"{len(present)} keys present but {stale_count} are >30min stale")
    elif len(present) >= 3:
        record_result("WARNING", 25, "Dashboard Cache",
                       f"Only {len(present)}/{len(expected_keys)} keys present")
    else:
        record_result("FAIL", 25, "Dashboard Cache",
                       f"Only {len(present)}/{len(expected_keys)} expected cache keys present")
        suggest("Cache refresh job may not be computing all required keys.")


# ── SECTION 26: NOTIFICATION CHANNELS ───────────────────────────

async def check_notifications(session) -> None:
    section_header(26, "NOTIFICATION CHANNELS")

    rows = await session.execute(
        text("""
            SELECT key, value FROM admin_settings
            WHERE key IN ('notify_macos', 'notify_email_self', 'notify_teams_self')
        """)
    )
    settings_rows = rows.fetchall()

    settings = get_settings()

    notify_config = {}
    if settings_rows:
        for r in settings_rows:
            try:
                val = r.value if isinstance(r.value, (bool, str)) else json.loads(json.dumps(r.value))
                notify_config[r.key] = val
            except Exception:
                notify_config[r.key] = r.value
        console.print("  Notification settings from admin_settings:")
        for k, v in notify_config.items():
            console.print(f"    {k}: {v}")
    else:
        console.print("  [dim]No notification settings in admin_settings, using .env defaults[/dim]")
        notify_config["notify_macos"] = getattr(settings, "notify_macos", True)
        notify_config["notify_email_self"] = getattr(settings, "notify_email_self", False)
        notify_config["notify_teams_self"] = getattr(settings, "notify_teams_self", False)
        for k, v in notify_config.items():
            console.print(f"    {k}: {v}")

    any_enabled = any(
        str(v).lower() in ("true", "1", "yes") if isinstance(v, (str, bool))
        else bool(v)
        for v in notify_config.values()
    )

    macos_enabled = False
    macos_val = notify_config.get("notify_macos")
    if macos_val is not None:
        if isinstance(macos_val, bool):
            macos_enabled = macos_val
        elif isinstance(macos_val, str):
            macos_enabled = macos_val.lower() in ("true", "1", "yes")
        else:
            macos_enabled = bool(macos_val)

    if macos_enabled:
        record_result("PASS", 26, "Notifications", "macOS notifications enabled")
    elif any_enabled:
        record_result("PASS", 26, "Notifications", "At least one notification channel enabled")
    elif notify_config:
        record_result("WARNING", 26, "Notifications", "All notification channels disabled")
        suggest("Enable at least macOS notifications in admin settings.")
    else:
        record_result("FAIL", 26, "Notifications", "Notification settings not found in config or admin_settings")
        suggest("Check config.py for notify_macos, notify_email_self, notify_teams_self fields.")


# ── SECTION 27: LLM COST TRACKING (Phase 4 tasks) ──────────────

async def check_llm_costs_phase4(session) -> None:
    section_header(27, "LLM COST TRACKING (Phase 4 tasks)")

    phase4_tasks = (
        'briefing', 'meeting_prep', 'monday_brief', 'friday_recap',
        'draft_generation', 'response_draft', 'voice_profile',
        'rag_chat', 'sentiment', 'readiness',
    )
    placeholders = ", ".join(f":t{i}" for i in range(len(phase4_tasks)))
    params = {f"t{i}": t for i, t in enumerate(phase4_tasks)}

    rows = await session.execute(
        text(f"""
            SELECT model, task, SUM(calls) as total_calls,
                   SUM(input_tokens) as input_tok, SUM(output_tokens) as output_tok
            FROM llm_usage
            WHERE task IN ({placeholders})
              AND date >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY model, task
            ORDER BY total_calls DESC
        """),
        params,
    )
    usage = rows.fetchall()

    if not usage:
        row = await session.execute(text("SELECT COUNT(*) FROM llm_usage"))
        total = row.scalar()
        if total == 0:
            console.print("  [red]llm_usage table is completely empty[/red]")
            record_result("FAIL", 27, "LLM Costs (Phase 4)", "llm_usage table is empty")
        else:
            console.print(f"  [yellow]No Phase 4 tasks in llm_usage (total rows: {total})[/yellow]")
            record_result("WARNING", 27, "LLM Costs (Phase 4)",
                           "Intelligence services not making LLM calls (or using different task names)")
            suggest("Check that briefing/draft/chat LLM calls upsert into llm_usage with correct task names.")
        return

    PRICING = {
        "haiku": (0.25 / 1_000_000, 1.25 / 1_000_000),
        "sonnet": (3.0 / 1_000_000, 15.0 / 1_000_000),
        "embedding": (0.02 / 1_000_000, 0.0),
    }

    table = Table(show_header=True, header_style="bold")
    table.add_column("Model")
    table.add_column("Task")
    table.add_column("Calls", justify="right")
    table.add_column("Input Tok", justify="right")
    table.add_column("Output Tok", justify="right")
    table.add_column("Est. Cost", justify="right")

    total_cost = 0.0
    for u in usage:
        model_lower = (u.model or "").lower()
        if "haiku" in model_lower:
            price = PRICING["haiku"]
        elif "sonnet" in model_lower:
            price = PRICING["sonnet"]
        elif "embed" in model_lower:
            price = PRICING["embedding"]
        else:
            price = PRICING["sonnet"]

        cost = (u.input_tok or 0) * price[0] + (u.output_tok or 0) * price[1]
        total_cost += cost

        table.add_row(
            u.model, u.task, str(u.total_calls),
            f"{u.input_tok:,}" if u.input_tok else "0",
            f"{u.output_tok:,}" if u.output_tok else "0",
            f"${cost:.4f}",
        )

    console.print(table)
    console.print(f"\n  Estimated Phase 4 7-day cost: [bold]${total_cost:.2f}[/bold]")

    if total_cost < 20:
        record_result("PASS", 27, "LLM Costs (Phase 4)", f"${total_cost:.2f}/week")
    elif total_cost <= 30:
        record_result("WARNING", 27, "LLM Costs (Phase 4)",
                       f"${total_cost:.2f}/week (higher than expected for Phase 4)")
    else:
        record_result("FAIL", 27, "LLM Costs (Phase 4)", f"${total_cost:.2f}/week (too high)")
        suggest("Check briefing and draft generation frequency. May be regenerating too often.")


# ── SECTION 28: END-TO-END FLOW CHECK ───────────────────────────

async def check_end_to_end(session) -> None:
    section_header(28, "END-TO-END FLOW CHECK")

    console.print("  [bold]Meeting pipeline completeness:[/bold]")
    meeting_rows = await session.execute(
        text("""
            SELECT m.title, m.start_time, m.transcript_status, m.processing_status,
                   (SELECT COUNT(*) FROM action_items ai WHERE ai.source_meeting_id = m.id) as action_items,
                   (SELECT COUNT(*) FROM decisions d WHERE d.source_meeting_id = m.id) as decisions,
                   (SELECT COUNT(*) FROM workstream_items wi WHERE wi.item_type = 'meeting' AND wi.item_id = m.id) as workstreams,
                   (SELECT COUNT(*) FROM briefings b WHERE b.related_meeting_id = m.id AND b.briefing_type = 'meeting_prep') as prep_briefs,
                   m.embedding IS NOT NULL as has_embedding
            FROM meetings m
            WHERE m.processing_status = 'completed'
            ORDER BY m.start_time DESC LIMIT 5
        """)
    )
    meetings = meeting_rows.fetchall()

    if not meetings:
        console.print("  [red]No completed meetings in pipeline[/red]")
    else:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Meeting", max_width=30)
        table.add_column("Actions", justify="right")
        table.add_column("Decisions", justify="right")
        table.add_column("Workstreams", justify="right")
        table.add_column("Prep Brief", justify="right")
        table.add_column("Embedding")

        for m in meetings:
            table.add_row(
                (m.title or "")[:30],
                str(m.action_items), str(m.decisions), str(m.workstreams),
                str(m.prep_briefs),
                "[green]Y[/green]" if m.has_embedding else "[red]N[/red]",
            )
        console.print(table)

    full_meeting = any(
        (m.action_items > 0 or m.decisions > 0) and m.workstreams > 0 and m.has_embedding
        for m in meetings
    ) if meetings else False

    console.print()
    console.print("  [bold]Email pipeline completeness:[/bold]")
    email_rows = await session.execute(
        text("""
            SELECT e.subject, e.email_class, e.triage_class, e.processing_status,
                   (SELECT COUNT(*) FROM email_asks ea WHERE ea.email_id = e.id) as asks,
                   (SELECT COUNT(*) FROM workstream_items wi WHERE wi.item_type = 'email' AND wi.item_id = e.id) as workstreams,
                   e.embedding IS NOT NULL as has_embedding
            FROM emails e
            WHERE e.email_class = 'human' AND e.triage_class = 'substantive'
            ORDER BY e.datetime DESC LIMIT 5
        """)
    )
    emails = email_rows.fetchall()

    if not emails:
        console.print("  [yellow]No substantive human emails to check[/yellow]")
    else:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Subject", max_width=35)
        table.add_column("Status")
        table.add_column("Asks", justify="right")
        table.add_column("Workstreams", justify="right")
        table.add_column("Embedding")

        for e in emails:
            table.add_row(
                (e.subject or "")[:35],
                e.processing_status or "?",
                str(e.asks), str(e.workstreams),
                "[green]Y[/green]" if e.has_embedding else "[red]N[/red]",
            )
        console.print(table)

    full_email = any(
        e.asks > 0 and e.workstreams > 0 and e.has_embedding
        for e in emails
    ) if emails else False

    if full_meeting and full_email:
        record_result("PASS", 28, "End-to-End Flow",
                       "Full pipeline confirmed for both meetings and emails")
    elif full_meeting or full_email:
        which = "meetings" if full_meeting else "emails"
        record_result("WARNING", 28, "End-to-End Flow",
                       f"Full pipeline confirmed for {which} only")
    elif meetings or emails:
        record_result("WARNING", 28, "End-to-End Flow",
                       "Items exist but none have completed all pipeline stages")
        suggest("Check extraction, workstream assignment, and embedding generation in sequence.")
    else:
        record_result("FAIL", 28, "End-to-End Flow", "No completed items in the pipeline")


# ══════════════════════════════════════════════════════════════════
#                    PART C: PHASE 5 CHECKS
# ══════════════════════════════════════════════════════════════════

# ── SECTION 29: ERROR HANDLING & RETRY LOGIC ─────────────────────

async def check_error_handling(session) -> None:
    section_header(29, "ERROR HANDLING & RECOVERY")

    # Check all services and their error history
    rows = await session.execute(
        text("""
            SELECT service, status, last_success, last_error, last_error_message
            FROM system_health
            ORDER BY service
        """)
    )
    services = rows.fetchall()

    if not services:
        console.print("  [red]system_health table is empty[/red]")
        record_result("FAIL", 29, "Error Handling", "system_health table is empty")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Service", min_width=22)
    table.add_column("Status")
    table.add_column("Last Success")
    table.add_column("Last Error")
    table.add_column("Error Message", max_width=40)

    has_down = False
    has_degraded = False
    recovered_count = 0

    now = datetime.now(timezone.utc)
    for svc in services:
        table.add_row(
            svc.service,
            svc.status or "?",
            time_ago(svc.last_success),
            time_ago(svc.last_error),
            (svc.last_error_message or "")[:40],
        )

        if svc.status == "down":
            # Check if it's been down for >1 hour
            if svc.last_success:
                ls = svc.last_success
                if ls.tzinfo is None:
                    from zoneinfo import ZoneInfo
                    ls = ls.replace(tzinfo=ZoneInfo("UTC"))
                if (now - ls).total_seconds() > 3600:
                    has_down = True
            else:
                has_down = True
        elif svc.status == "degraded":
            has_degraded = True

        # Service has had errors but recovered
        if svc.last_error and svc.status == "healthy":
            recovered_count += 1

    console.print(table)

    if recovered_count > 0:
        console.print(f"\n  [green]Services that recovered from errors: {recovered_count}[/green]")

    # Check for currently degraded/down services
    degraded_rows = await session.execute(
        text("""
            SELECT service, status, last_error_message
            FROM system_health
            WHERE status IN ('degraded', 'down')
        """)
    )
    degraded = degraded_rows.fetchall()

    if degraded:
        console.print(f"\n  [yellow]Services currently in error state: {len(degraded)}[/yellow]")
        for d in degraded:
            console.print(f"    [yellow]- {d.service}: {d.status} -- {(d.last_error_message or 'no message')[:60]}[/yellow]")

    if has_down:
        record_result("FAIL", 29, "Error Handling", "One or more services down for >1 hour")
        suggest("Check logs for the down service. May need manual restart.")
    elif has_degraded:
        record_result("WARNING", 29, "Error Handling", "Services degraded but may self-recover")
    else:
        detail = "All services healthy"
        if recovered_count > 0:
            detail += f" ({recovered_count} recovered from past errors)"
        record_result("PASS", 29, "Error Handling", detail)


# ── SECTION 30: GRAPH API RATE LIMIT HANDLING ────────────────────

async def check_rate_limit_handling(session) -> None:
    section_header(30, "GRAPH API RATE LIMIT HANDLING")

    rows = await session.execute(
        text("""
            SELECT service, last_error_message, status
            FROM system_health
            WHERE last_error_message ILIKE '%429%'
               OR last_error_message ILIKE '%rate%'
               OR last_error_message ILIKE '%throttl%'
               OR last_error_message ILIKE '%Retry-After%'
        """)
    )
    rate_limit_events = rows.fetchall()

    if rate_limit_events:
        console.print(f"  Rate limit events detected: [bold]{len(rate_limit_events)}[/bold]")
        for evt in rate_limit_events:
            status_str = "[green]recovered[/green]" if evt.status == "healthy" else f"[yellow]{evt.status}[/yellow]"
            console.print(f"    - {evt.service}: {status_str} -- {(evt.last_error_message or '')[:60]}")

        all_recovered = all(evt.status == "healthy" for evt in rate_limit_events)
        if all_recovered:
            record_result("PASS", 30, "Rate Limit Handling",
                           f"{len(rate_limit_events)} rate limit events, all services recovered (handled correctly)")
        elif any(evt.status == "down" for evt in rate_limit_events):
            record_result("FAIL", 30, "Rate Limit Handling",
                           "Rate limit errors with service down (retry logic may be missing)")
            suggest("Add exponential backoff with Retry-After header handling to graph_client.py.")
        else:
            record_result("WARNING", 30, "Rate Limit Handling",
                           "Rate limit errors with service degraded (handling exists but imperfect)")
    else:
        console.print("  [dim]No rate limit events detected in system_health[/dim]")

        # Code inspection hint
        graph_client_path = os.path.join("aegis", "ingestion", "graph_client.py")
        if os.path.exists(graph_client_path):
            try:
                with open(graph_client_path, "r") as f:
                    content = f.read()
                has_retry = "retry" in content.lower() or "Retry-After" in content or "429" in content
                if has_retry:
                    console.print("  [green]graph_client.py contains retry/429 handling code[/green]")
                else:
                    console.print("  [yellow]graph_client.py may not have retry logic for 429 responses[/yellow]")
            except Exception:
                pass

        record_result("PASS", 30, "Rate Limit Handling",
                       "No rate limit events detected -- cannot confirm retry logic works (consider a load test)")

    if VERBOSE:
        suggest("To test rate limit handling: rapidly hit Graph API endpoints to trigger 429 responses.")


# ── SECTION 31: TOKEN SECURITY ───────────────────────────────────

async def check_token_security(session) -> None:
    section_header(31, "TOKEN SECURITY")

    token_path = os.path.expanduser("~/.aegis/msal_token_cache.json")

    if not os.path.exists(token_path):
        console.print(f"  [red]Token cache not found at {token_path}[/red]")
        record_result("FAIL", 31, "Token Security", f"Token cache not found at {token_path}")
        suggest("Run the OAuth setup flow to create the token cache.")
        return

    mode = os.stat(token_path).st_mode
    permissions = oct(mode)[-3:]
    owner_only = (mode & 0o077) == 0  # no group/other permissions

    console.print(f"  Token cache: {token_path}")
    console.print(f"  Permissions: {permissions}")

    if owner_only:
        console.print("  [green]Permissions are restrictive (owner-only read/write)[/green]")
        record_result("PASS", 31, "Token Security", f"Token cache exists with permissions {permissions} (owner-only)")
    else:
        console.print(f"  [yellow]Permissions {permissions} are too open -- should be 600[/yellow]")
        record_result("WARNING", 31, "Token Security",
                       f"Token cache permissions are {permissions} -- should be 600 (owner-only)")
        suggest(f"Run: chmod 600 {token_path}")

    # Also check .env is gitignored
    gitignore_path = ".gitignore"
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r") as f:
            gitignore = f.read()
        if ".env" in gitignore:
            console.print("  [green].env is in .gitignore[/green]")
        else:
            console.print("  [yellow].env is NOT in .gitignore -- secrets at risk[/yellow]")

    if VERBOSE:
        file_size = os.path.getsize(token_path)
        mtime = datetime.fromtimestamp(os.path.getmtime(token_path))
        console.print(f"  [dim]File size: {file_size} bytes, last modified: {mtime}[/dim]")


# ── SECTION 32: PII-SAFE LOGGING ────────────────────────────────

async def check_pii_logging(session) -> None:
    section_header(32, "PII-SAFE LOGGING")

    # Find log files
    log_dirs = ["logs", "."]
    log_files = []
    for d in log_dirs:
        log_files.extend(glob_mod.glob(os.path.join(d, "*.log")))
        log_files.extend(glob_mod.glob(os.path.join(d, "*.log.*")))
    log_files.extend(glob_mod.glob("logs/**/*.log", recursive=True))

    # Deduplicate
    log_files = list(set(log_files))

    if not log_files:
        console.print("  [yellow]No log files found -- logging may not be configured[/yellow]")
        record_result("WARNING", 32, "PII-Safe Logging", "No log files found -- logging may not be configured")
        suggest("Configure logging with file handler in main.py or config.py.")
        return

    console.print(f"  Log files found: {len(log_files)}")

    pii_patterns = [
        (r'body["\s]*[:=]', "body content reference"),
        (r'transcript_text', "raw transcript reference"),
        (r'body_text', "raw email body reference"),
        (r'"content"\s*:.*@', "email address in content"),
        (r'(?:password|secret|token)\s*[:=]\s*["\']?\w', "credential pattern"),
    ]

    total_violations = 0
    violation_details = []

    for log_file in log_files:
        try:
            with open(log_file, "r", errors="ignore") as f:
                # Read last 500 lines
                lines = f.readlines()
                tail_lines = lines[-500:] if len(lines) > 500 else lines

                for line_num, line in enumerate(tail_lines, start=max(1, len(lines) - 500 + 1)):
                    # Skip lines that are just config references
                    if "config" in line.lower() and "=" in line:
                        continue
                    for pattern, desc in pii_patterns:
                        if re.search(pattern, line, re.IGNORECASE):
                            total_violations += 1
                            violation_details.append((log_file, line_num, desc))
                            if total_violations >= 20:
                                break
                    if total_violations >= 20:
                        break
        except Exception:
            continue

    if total_violations == 0:
        console.print("  [green]No PII patterns found in log files[/green]")
        record_result("PASS", 32, "PII-Safe Logging", "No PII patterns detected in log files")
    elif total_violations <= 3:
        console.print(f"  [yellow]Possible PII detected: {total_violations} matches[/yellow]")
        for log_file, line_num, desc in violation_details:
            console.print(f"    [yellow]- {log_file}:{line_num} -- {desc}[/yellow]")
        record_result("WARNING", 32, "PII-Safe Logging",
                       f"{total_violations} possible PII patterns detected")
        suggest("Review flagged log lines and add PII-safe formatting.")
    else:
        console.print(f"  [red]PII detected: {total_violations} matches[/red]")
        for log_file, line_num, desc in violation_details[:10]:
            console.print(f"    [red]- {log_file}:{line_num} -- {desc}[/red]")
        record_result("FAIL", 32, "PII-Safe Logging",
                       f"{total_violations} PII patterns in logs (email bodies, transcripts, or credentials)")
        suggest("Add PII-safe log formatter. Never log raw content -- metadata only.")


# ── SECTION 33: DATABASE BACKUP ──────────────────────────────────

async def check_database_backup(session) -> None:
    section_header(33, "DATABASE BACKUP")

    backup_dir = os.path.expanduser("~/.aegis/backups/")

    if not os.path.isdir(backup_dir):
        console.print(f"  [red]Backup directory does not exist: {backup_dir}[/red]")
        record_result("FAIL", 33, "Database Backup", f"Backup directory {backup_dir} does not exist")
        suggest(f"Create it: mkdir -p {backup_dir} && implement daily pg_dump backup script.")
        return

    backup_patterns = ["*.sql", "*.sql.gz", "*.dump", "*.sql.bz2", "*.backup"]
    backups = []
    for pattern in backup_patterns:
        backups.extend(glob_mod.glob(os.path.join(backup_dir, pattern)))
    backups = sorted(set(backups))

    if not backups:
        console.print(f"  [red]No backup files found in {backup_dir}[/red]")
        record_result("FAIL", 33, "Database Backup", "No backup files exist")
        suggest("Implement daily pg_dump backup: pg_dump -h localhost -p 5434 -U postgres aegis > backup.sql")
        return

    latest = backups[-1]
    latest_mtime = datetime.fromtimestamp(os.path.getmtime(latest))
    age_hours = (datetime.now() - latest_mtime).total_seconds() / 3600
    size_mb = os.path.getsize(latest) / (1024 * 1024)
    backup_count = len(backups)

    console.print(f"  Backup directory: {backup_dir}")
    console.print(f"  Backup count: [bold]{backup_count}[/bold]")
    console.print(f"  Latest backup: {os.path.basename(latest)}")
    console.print(f"  Latest age: [bold]{age_hours:.1f} hours[/bold]")
    console.print(f"  Latest size: [bold]{size_mb:.2f} MB[/bold]")

    if VERBOSE:
        console.print()
        console.print("  [dim]All backups:[/dim]")
        for b in backups:
            b_mtime = datetime.fromtimestamp(os.path.getmtime(b))
            b_size = os.path.getsize(b) / (1024 * 1024)
            console.print(f"    [dim]- {os.path.basename(b)}: {b_mtime.strftime('%Y-%m-%d %H:%M')}, {b_size:.2f} MB[/dim]")

    if age_hours < 28 and size_mb > 0.1 and backup_count <= 30:
        record_result("PASS", 33, "Database Backup",
                       f"Latest is {age_hours:.0f}h old, {size_mb:.1f}MB, {backup_count} files")
    elif age_hours > 48:
        record_result("WARNING", 33, "Database Backup",
                       f"Latest backup is {age_hours:.0f} hours old (>48h -- missed a day?)")
        suggest("Check the backup cron/LaunchAgent. May need to restart.")
    elif size_mb < 0.01:
        record_result("WARNING", 33, "Database Backup",
                       f"Latest backup is suspiciously small ({size_mb:.3f} MB)")
        suggest("Backup may be empty or corrupted. Verify with pg_restore --list.")
    elif backup_count > 30:
        record_result("WARNING", 33, "Database Backup",
                       f"{backup_count} backups -- rotation not working (expected max 30)")
        suggest("Add rotation logic: delete backups older than 30 days.")
    else:
        record_result("PASS", 33, "Database Backup",
                       f"Backup exists, {age_hours:.0f}h old, {size_mb:.1f}MB")


# ── SECTION 34: DATA RETENTION ───────────────────────────────────

async def check_data_retention(session) -> None:
    section_header(34, "DATA RETENTION")

    rows = await session.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE datetime > NOW() - INTERVAL '90 days') as hot,
                COUNT(*) FILTER (WHERE datetime BETWEEN NOW() - INTERVAL '365 days' AND NOW() - INTERVAL '90 days') as warm,
                COUNT(*) FILTER (WHERE datetime < NOW() - INTERVAL '365 days') as cold,
                COUNT(*) as total
            FROM emails
        """)
    )
    r = rows.first()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Tier")
    table.add_column("Range")
    table.add_column("Emails", justify="right")

    table.add_row("Hot", "< 90 days", str(r.hot))
    table.add_row("Warm", "90-365 days", str(r.warm))
    table.add_row("Cold", "> 365 days", str(r.cold))
    table.add_row("[bold]Total[/bold]", "", f"[bold]{r.total}[/bold]")

    console.print(table)

    # Check meeting age distribution too
    meeting_rows = await session.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE start_time > NOW() - INTERVAL '90 days') as hot,
                COUNT(*) FILTER (WHERE start_time < NOW() - INTERVAL '90 days') as older,
                COUNT(*) as total
            FROM meetings
        """)
    )
    mr = meeting_rows.first()
    console.print(f"\n  Meetings: {mr.hot} hot / {mr.older} older / {mr.total} total")

    # Check if retention job is likely registered
    # Look for scheduler config references
    scheduler_path = os.path.join("aegis", "intelligence", "scheduler.py")
    has_retention_job = False
    if os.path.exists(scheduler_path):
        try:
            with open(scheduler_path, "r") as f:
                content = f.read()
            has_retention_job = "retention" in content.lower()
        except Exception:
            pass

    if has_retention_job:
        console.print("  [green]Retention job found in scheduler.py[/green]")

    # Determine status
    system_age_days = 0
    if r.total > 0:
        oldest_row = await session.execute(
            text("SELECT MIN(datetime) FROM emails")
        )
        oldest = oldest_row.scalar()
        if oldest:
            if oldest.tzinfo is None:
                from zoneinfo import ZoneInfo
                oldest = oldest.replace(tzinfo=ZoneInfo("UTC"))
            system_age_days = (datetime.now(timezone.utc) - oldest).days

    console.print(f"  System data age: ~{system_age_days} days")

    if has_retention_job:
        record_result("PASS", 34, "Data Retention",
                       f"Retention job exists; data: {r.hot} hot, {r.warm} warm, {r.cold} cold")
    elif system_age_days < 90:
        record_result("PASS", 34, "Data Retention",
                       f"System <90 days old ({system_age_days}d) -- retention not yet needed")
    else:
        record_result("WARNING", 34, "Data Retention",
                       f"System is {system_age_days} days old but no retention job found")
        suggest("Add a retention job to scheduler.py to manage hot/warm/cold tiers.")


# ── SECTION 35: ADMIN SETTINGS ───────────────────────────────────

async def check_admin_settings(session) -> None:
    section_header(35, "ADMIN SETTINGS")

    row = await session.execute(
        text("""
            SELECT COUNT(*) as total_settings,
                   COUNT(DISTINCT key) as unique_keys
            FROM admin_settings
        """)
    )
    r = row.first()

    console.print(f"  Total settings: [bold]{r.total_settings}[/bold]")
    console.print(f"  Unique keys: [bold]{r.unique_keys}[/bold]")

    if r.total_settings == 0:
        console.print("  [red]admin_settings table is empty[/red]")
        record_result("FAIL", 35, "Admin Settings", "admin_settings table is empty (admin page cannot override .env)")
        suggest("Seed admin_settings with defaults from config.py on startup.")
        return

    # Sample settings
    if VERBOSE:
        sample_rows = await session.execute(
            text("""
                SELECT key, LEFT(value::text, 50) as value_preview, updated
                FROM admin_settings
                ORDER BY key LIMIT 20
            """)
        )
        samples = sample_rows.fetchall()
        console.print()
        for s in samples:
            console.print(f"    [dim]{s.key}: {s.value_preview} (updated: {time_ago(s.updated)})[/dim]")

    # Category coverage
    cat_rows = await session.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE key LIKE 'polling_%') as polling,
                COUNT(*) FILTER (WHERE key LIKE 'triage_%') as triage,
                COUNT(*) FILTER (WHERE key LIKE 'workstream_%') as workstream,
                COUNT(*) FILTER (WHERE key LIKE 'stale_%') as stale,
                COUNT(*) FILTER (WHERE key LIKE 'notify_%') as notifications,
                COUNT(*) FILTER (WHERE key LIKE 'retention_%') as retention,
                COUNT(*) FILTER (WHERE key LIKE 'sentiment_%') as sentiment,
                COUNT(*) FILTER (WHERE key LIKE 'meeting_%') as meeting
            FROM admin_settings
        """)
    )
    cats = cat_rows.first()

    category_counts = {
        "polling": cats.polling,
        "triage": cats.triage,
        "workstream": cats.workstream,
        "stale": cats.stale,
        "notifications": cats.notifications,
        "retention": cats.retention,
        "sentiment": cats.sentiment,
        "meeting": cats.meeting,
    }

    categories_present = sum(1 for v in category_counts.values() if v > 0)

    console.print(f"\n  Categories with settings: [bold]{categories_present}/8[/bold]")
    for cat, cnt in category_counts.items():
        icon = "[green]Y[/green]" if cnt > 0 else "[red]N[/red]"
        console.print(f"    {icon} {cat}: {cnt}")

    # Overall assessment
    if r.unique_keys >= 30 and categories_present >= 6:
        record_result("PASS", 35, "Admin Settings",
                       f"{r.unique_keys} settings across {categories_present} categories")
    elif r.unique_keys >= 10 and categories_present >= 3:
        record_result("WARNING", 35, "Admin Settings",
                       f"Partial: {r.unique_keys} settings, {categories_present} categories (expected 30+, 6+)")
    else:
        record_result("WARNING", 35, "Admin Settings",
                       f"Only {r.unique_keys} settings in {categories_present} categories")
        suggest("Ensure admin page seeds all ~70 config values into admin_settings on startup.")


# ── SECTION 36: ADMIN SETTINGS OVERRIDE ──────────────────────────

async def check_admin_override(session) -> None:
    section_header(36, "ADMIN SETTINGS OVERRIDE LOGIC")

    # Code inspection: check if config.py has admin_settings override logic
    config_path = os.path.join("aegis", "config.py")
    main_path = os.path.join("aegis", "main.py")

    has_override_logic = False
    found_in = []

    for filepath in [config_path, main_path]:
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    content = f.read()
                if "admin_settings" in content.lower():
                    found_in.append(filepath)
                    if any(kw in content.lower() for kw in ["override", "load_admin", "apply_admin", "merge"]):
                        has_override_logic = True
            except Exception:
                pass

    # Also check if the route handler reads/writes admin_settings
    admin_route_path = os.path.join("aegis", "web", "routes", "admin.py")
    admin_route_exists = os.path.exists(admin_route_path)
    if admin_route_exists:
        try:
            with open(admin_route_path, "r") as f:
                content = f.read()
            if "admin_settings" in content:
                found_in.append(admin_route_path)
        except Exception:
            pass

    console.print(f"  admin_settings referenced in: {', '.join(found_in) if found_in else 'nowhere'}")
    console.print(f"  Override logic detected: {'[green]yes[/green]' if has_override_logic else '[yellow]no[/yellow]'}")
    console.print(f"  Admin route exists: {'[green]yes[/green]' if admin_route_exists else '[red]no[/red]'}")

    # Verify by checking if a known setting in admin_settings differs from .env default
    row = await session.execute(
        text("""
            SELECT key, value FROM admin_settings LIMIT 1
        """)
    )
    has_any_setting = row.first() is not None

    if has_override_logic and has_any_setting:
        record_result("PASS", 36, "Admin Override",
                       "Config has admin_settings override logic and settings exist")
    elif has_any_setting and found_in:
        record_result("WARNING", 36, "Admin Override",
                       "admin_settings table has data and code references it, but explicit override logic unclear")
        suggest("Verify that config.py or main.py reads admin_settings and applies values at runtime.")
    elif admin_route_exists and has_any_setting:
        record_result("WARNING", 36, "Admin Override",
                       "Admin route and settings exist but override mechanism not confirmed in config.py")
    else:
        record_result("FAIL", 36, "Admin Override",
                       "No admin_settings override mechanism found (admin page writes to DB but nothing reads it)")
        suggest("Add logic in config.py or main.py to load admin_settings and override .env defaults.")


# ── SECTION 37: CRASH RECOVERY ───────────────────────────────────

async def check_crash_recovery(session) -> None:
    section_header(37, "CRASH RECOVERY (stuck processing items)")

    rows = await session.execute(
        text("""
            SELECT 'meetings' as type, COUNT(*) as stuck
            FROM meetings WHERE processing_status = 'processing'
            UNION ALL
            SELECT 'emails', COUNT(*)
            FROM emails WHERE processing_status = 'processing'
            UNION ALL
            SELECT 'chat_messages', COUNT(*)
            FROM chat_messages WHERE processing_status = 'processing'
        """)
    )
    stuck = rows.fetchall()

    total_stuck = sum(s.stuck for s in stuck)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Type")
    table.add_column("Stuck in 'processing'", justify="right")

    for s in stuck:
        style = "[red]" if s.stuck > 0 else "[green]"
        table.add_row(s.type, f"{style}{s.stuck}[/{style[1:]}")

    console.print(table)

    # Also check if startup recovery logic exists
    main_path = os.path.join("aegis", "main.py")
    has_recovery_logic = False
    if os.path.exists(main_path):
        try:
            with open(main_path, "r") as f:
                content = f.read()
            has_recovery_logic = (
                "processing" in content and
                ("pending" in content or "reset" in content.lower() or "stuck" in content.lower())
            )
        except Exception:
            pass

    console.print(f"\n  Startup recovery logic in main.py: "
                  f"{'[green]yes[/green]' if has_recovery_logic else '[yellow]not detected[/yellow]'}")

    if total_stuck == 0:
        record_result("PASS", 37, "Crash Recovery",
                       "Zero items stuck in processing"
                       + (" (recovery logic exists)" if has_recovery_logic else ""))
    elif total_stuck <= 3:
        record_result("WARNING", 37, "Crash Recovery",
                       f"{total_stuck} items stuck in 'processing' -- recovery may not be running")
        suggest("Check main.py startup: should reset processing_status='processing' to 'pending'.")
    else:
        record_result("FAIL", 37, "Crash Recovery",
                       f"{total_stuck} items stuck in 'processing' -- recovery is not implemented")
        suggest("Add startup hook: UPDATE SET processing_status='pending' WHERE processing_status='processing'.")


# ── SECTION 38: STARTUP SCRIPT ───────────────────────────────────

async def check_startup_script(session) -> None:
    section_header(38, "STARTUP SCRIPT & LAUNCHAGENT")

    # Check for startup script
    startup_paths = [
        "scripts/aegis",
        "aegis",
        "scripts/start.sh",
        "start.sh",
        "scripts/aegis.sh",
    ]
    startup_found = None
    for sp in startup_paths:
        if os.path.exists(sp):
            startup_found = sp
            break

    if startup_found:
        console.print(f"  Startup script found: [green]{startup_found}[/green]")
        # Check if executable
        if os.access(startup_found, os.X_OK):
            console.print("  Executable: [green]yes[/green]")
        else:
            console.print("  Executable: [yellow]no -- needs chmod +x[/yellow]")
    else:
        console.print("  [yellow]No startup script found[/yellow]")

    # Check for LaunchAgent plist
    plist_path = os.path.expanduser("~/Library/LaunchAgents/com.aegis.app.plist")
    plist_exists = os.path.exists(plist_path)

    if plist_exists:
        console.print(f"  LaunchAgent plist: [green]{plist_path}[/green]")
    else:
        console.print(f"  LaunchAgent plist: [yellow]not found at {plist_path}[/yellow]")

    if startup_found and plist_exists:
        record_result("PASS", 38, "Startup Script",
                       f"Script: {startup_found}, LaunchAgent: present")
    elif startup_found:
        record_result("WARNING", 38, "Startup Script",
                       f"Script exists ({startup_found}) but no LaunchAgent (manual start on reboot)")
        suggest(f"Create {plist_path} to auto-start Aegis on macOS boot.")
    elif plist_exists:
        record_result("WARNING", 38, "Startup Script",
                       "LaunchAgent exists but no startup script found")
    else:
        record_result("FAIL", 38, "Startup Script", "Neither startup script nor LaunchAgent exists")
        suggest("Create scripts/aegis (start/stop) and a LaunchAgent plist for auto-start.")


# ── SECTION 39: LOGGING ──────────────────────────────────────────

async def check_logging(session) -> None:
    section_header(39, "LOGGING")

    log_dir = "logs"
    log_dir_exists = os.path.isdir(log_dir)

    if not log_dir_exists:
        console.print(f"  [yellow]Log directory '{log_dir}' does not exist[/yellow]")
        record_result("FAIL", 39, "Logging", "No logs directory found")
        suggest("Configure logging with file handler: logs/aegis.log with rotation.")
        return

    log_files = glob_mod.glob(os.path.join(log_dir, "*.log*"))
    log_files = list(set(log_files))

    if not log_files:
        console.print(f"  [yellow]Log directory exists but no .log files found[/yellow]")
        record_result("WARNING", 39, "Logging", "Log directory exists but empty")
        suggest("Check logging configuration -- file handler may not be writing.")
        return

    total_size = sum(os.path.getsize(f) for f in log_files)
    rotated = [f for f in log_files if '.log.' in f or f.endswith('.gz') or f.endswith('.bz2')]

    console.print(f"  Log directory: {log_dir}")
    console.print(f"  Log files: [bold]{len(log_files)}[/bold]")
    console.print(f"  Total log size: [bold]{total_size / (1024 * 1024):.2f} MB[/bold]")
    console.print(f"  Rotated files: [bold]{len(rotated)}[/bold]")

    if VERBOSE:
        console.print()
        for lf in sorted(log_files):
            size_kb = os.path.getsize(lf) / 1024
            mtime = datetime.fromtimestamp(os.path.getmtime(lf))
            console.print(f"    [dim]- {os.path.basename(lf)}: {size_kb:.1f} KB, "
                          f"modified: {mtime.strftime('%Y-%m-%d %H:%M')}[/dim]")

    # Check for rotation configuration in code
    has_rotation_config = False
    for filepath in ["aegis/main.py", "aegis/config.py"]:
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    content = f.read()
                if any(kw in content for kw in [
                    "RotatingFileHandler", "TimedRotatingFileHandler",
                    "maxBytes", "backupCount", "rotation",
                ]):
                    has_rotation_config = True
                    break
            except Exception:
                pass

    if log_files and (rotated or has_rotation_config):
        record_result("PASS", 39, "Logging",
                       f"{len(log_files)} log files, rotation {'configured' if has_rotation_config else 'active'}")
    elif log_files and not rotated and not has_rotation_config:
        record_result("WARNING", 39, "Logging",
                       f"{len(log_files)} log files but no rotation configured (will grow unbounded)")
        suggest("Add RotatingFileHandler or TimedRotatingFileHandler to logging config.")
    else:
        record_result("PASS", 39, "Logging", f"{len(log_files)} log files present")


# ── SECTION 40: SEARCH FUNCTIONALITY ────────────────────────────

async def check_search(session) -> None:
    section_header(40, "SEARCH FUNCTIONALITY")

    rows = await session.execute(
        text("""
            SELECT 'meetings' as type, COUNT(*) FILTER (WHERE embedding IS NOT NULL) as searchable
            FROM meetings
            UNION ALL
            SELECT 'emails', COUNT(*) FILTER (WHERE embedding IS NOT NULL)
            FROM emails
            UNION ALL
            SELECT 'chat_messages', COUNT(*) FILTER (WHERE embedding IS NOT NULL)
            FROM chat_messages
            UNION ALL
            SELECT 'action_items', COUNT(*) FILTER (WHERE embedding IS NOT NULL)
            FROM action_items
        """)
    )
    search_data = rows.fetchall()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Type")
    table.add_column("Searchable Items", justify="right")

    total_searchable = 0
    for s in search_data:
        table.add_row(s.type, str(s.searchable))
        total_searchable += s.searchable

    console.print(table)
    console.print(f"\n  Total searchable items: [bold]{total_searchable}[/bold]")

    # Check if search route exists
    search_route = os.path.join("aegis", "web", "routes", "search.py")
    search_exists = os.path.exists(search_route)
    console.print(f"  Search route: {'[green]exists[/green]' if search_exists else '[yellow]not found[/yellow]'}")

    if total_searchable > 200:
        record_result("PASS", 40, "Search", f"{total_searchable} searchable items across types")
    elif total_searchable >= 50:
        record_result("WARNING", 40, "Search", f"Only {total_searchable} searchable items (expected >200)")
    else:
        record_result("FAIL", 40, "Search", f"Only {total_searchable} searchable items (expected >200)")
        suggest("Fix embedding generation first (Section 12). Search depends on embeddings.")


# ── SECTION 41: MOBILE RESPONSIVENESS ────────────────────────────

async def check_mobile_responsive(session) -> None:
    section_header(41, "MOBILE RESPONSIVENESS")

    template_dir = os.path.join("aegis", "web", "templates")
    if not os.path.isdir(template_dir):
        console.print(f"  [red]Templates directory not found: {template_dir}[/red]")
        record_result("FAIL", 41, "Mobile Responsive", "Templates directory not found")
        return

    templates = glob_mod.glob(os.path.join(template_dir, "**", "*.html"), recursive=True)

    if not templates:
        console.print("  [red]No HTML templates found[/red]")
        record_result("FAIL", 41, "Mobile Responsive", "No HTML templates found")
        return

    responsive_count = 0
    non_responsive = []
    responsive_prefixes = ["sm:", "md:", "lg:", "xl:", "2xl:"]

    for t in templates:
        try:
            with open(t, "r") as f:
                content = f.read()
            if any(prefix in content for prefix in responsive_prefixes):
                responsive_count += 1
            else:
                # Only flag main pages, not small partials
                basename = os.path.basename(t)
                if not basename.startswith("_") and "component" not in t.lower():
                    non_responsive.append(basename)
        except Exception:
            continue

    total = len(templates)
    pct = responsive_count / total * 100 if total else 0

    console.print(f"  Templates scanned: [bold]{total}[/bold]")
    console.print(f"  With responsive breakpoints: [bold]{responsive_count}[/bold] ({pct:.0f}%)")

    if non_responsive and VERBOSE:
        console.print()
        console.print("  [dim]Templates without responsive classes:[/dim]")
        for nr in non_responsive[:10]:
            console.print(f"    [dim]- {nr}[/dim]")

    if pct >= 70:
        record_result("PASS", 41, "Mobile Responsive", f"{pct:.0f}% of templates use responsive breakpoints")
    elif pct >= 30:
        record_result("WARNING", 41, "Mobile Responsive",
                       f"Only {pct:.0f}% of templates use responsive breakpoints")
        suggest("Add Tailwind responsive classes (sm:/md:/lg:) to remaining templates.")
    else:
        record_result("FAIL", 41, "Mobile Responsive",
                       f"Only {pct:.0f}% of templates use responsive breakpoints (mobile layout will be broken)")
        suggest("Mobile-responsive audit needed. Add Tailwind breakpoints to all page templates.")


# ── SECTION 42: DOCKER & DATABASE HEALTH ─────────────────────────

async def check_docker_db_health(session) -> None:
    section_header(42, "DOCKER & DATABASE HEALTH")

    # Check Docker container
    container_status = "unknown"
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=aegis-db", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=5,
        )
        container_status = result.stdout.strip() or "not running"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        container_status = "docker command failed"

    is_running = "Up" in container_status
    console.print(f"  Docker container: {'[green]' if is_running else '[red]'}{container_status}[/{'green' if is_running else 'red'}]")

    if not is_running:
        record_result("FAIL", 42, "Docker & DB Health", f"Container not running: {container_status}")
        suggest("Run: docker compose up -d")
        return

    # Database connectivity + stats
    try:
        row = await session.execute(
            text("SELECT pg_database_size('aegis') as db_size_bytes")
        )
        db_size = row.scalar()
        db_size_mb = (db_size or 0) / (1024 * 1024)
        db_size_gb = db_size_mb / 1024
        console.print(f"  Database size: [bold]{db_size_mb:.1f} MB[/bold]")
    except Exception as e:
        console.print(f"  [red]Database size query failed: {e}[/red]")
        db_size_gb = 0

    # Active connections
    try:
        row = await session.execute(
            text("SELECT COUNT(*) FROM pg_stat_activity WHERE datname = 'aegis'")
        )
        active_conns = row.scalar()
        console.print(f"  Active connections: [bold]{active_conns}[/bold]")
    except Exception:
        active_conns = 0

    # pgvector
    try:
        row = await session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        pgvector_version = row.scalar()
        if pgvector_version:
            console.print(f"  pgvector version: [green]{pgvector_version}[/green]")
        else:
            console.print("  pgvector: [red]not installed[/red]")
    except Exception:
        pgvector_version = None
        console.print("  pgvector: [red]query failed[/red]")

    # Table sizes
    try:
        table_rows = await session.execute(
            text("""
                SELECT relname as table_name,
                       pg_size_pretty(pg_total_relation_size(relid)) as total_size,
                       n_live_tup as row_count
                FROM pg_stat_user_tables
                ORDER BY pg_total_relation_size(relid) DESC LIMIT 15
            """)
        )
        tables = table_rows.fetchall()

        if tables:
            console.print()
            size_table = Table(show_header=True, header_style="bold")
            size_table.add_column("Table")
            size_table.add_column("Size", justify="right")
            size_table.add_column("Rows", justify="right")

            for t in tables:
                size_table.add_row(t.table_name, t.total_size, f"{t.row_count:,}")

            console.print(size_table)
    except Exception as e:
        console.print(f"  [yellow]Table size query failed: {e}[/yellow]")

    # Overall assessment
    checks = []
    checks.append(is_running)
    checks.append(pgvector_version is not None)
    checks.append(db_size_gb < 5)

    if all(checks):
        detail = f"Container running, pgvector {pgvector_version}, DB size {db_size_mb:.0f} MB"
        if db_size_gb >= 5:
            record_result("WARNING", 42, "Docker & DB Health",
                           f"DB size {db_size_gb:.1f} GB (getting large, consider retention)")
        else:
            record_result("PASS", 42, "Docker & DB Health", detail)
    elif not pgvector_version:
        record_result("FAIL", 42, "Docker & DB Health", "pgvector extension not installed")
        suggest("Run: CREATE EXTENSION IF NOT EXISTS vector;")
    else:
        record_result("WARNING", 42, "Docker & DB Health",
                       f"DB size: {db_size_gb:.1f} GB -- may need retention/cleanup")


# ══════════════════════════════════════════════════════════════════
# SUMMARY SCORECARD
# ══════════════════════════════════════════════════════════════════

def print_summary(elapsed: float) -> None:
    console.print()
    console.rule("[bold]FULL SYSTEM VERIFICATION SUMMARY[/bold]")
    console.print()

    part_a = [r for r in results if r[1] <= 14]
    part_b = [r for r in results if 15 <= r[1] <= 28]
    part_c = [r for r in results if r[1] >= 29]

    def count_status(items):
        passed = len([r for r in items if r[0] == "PASS"])
        warnings = len([r for r in items if r[0] == "WARNING"])
        failures = len([r for r in items if r[0] == "FAIL"])
        return passed, warnings, failures, len(items)

    pa_pass, pa_warn, pa_fail, pa_total = count_status(part_a)
    pb_pass, pb_warn, pb_fail, pb_total = count_status(part_b)
    pc_pass, pc_warn, pc_fail, pc_total = count_status(part_c)
    all_pass, all_warn, all_fail, all_total = count_status(results)

    console.print("  [bold]PART A -- Phase 3 Re-Checks (Sections 1-14):[/bold]")
    console.print(f"    [green]PASSED:[/green]   {pa_pass:>2d} / {pa_total}")
    console.print(f"    [yellow]WARNINGS:[/yellow] {pa_warn:>2d}")
    console.print(f"    [red]FAILED:[/red]   {pa_fail:>2d}")

    console.print()
    console.print("  [bold]PART B -- Phase 4 Re-Checks (Sections 15-28):[/bold]")
    console.print(f"    [green]PASSED:[/green]   {pb_pass:>2d} / {pb_total}")
    console.print(f"    [yellow]WARNINGS:[/yellow] {pb_warn:>2d}")
    console.print(f"    [red]FAILED:[/red]   {pb_fail:>2d}")

    console.print()
    console.print("  [bold]PART C -- Phase 5 Checks (Sections 29-42):[/bold]")
    console.print(f"    [green]PASSED:[/green]   {pc_pass:>2d} / {pc_total}")
    console.print(f"    [yellow]WARNINGS:[/yellow] {pc_warn:>2d}")
    console.print(f"    [red]FAILED:[/red]   {pc_fail:>2d}")

    console.print()
    console.print("  [bold]COMBINED:[/bold]")
    console.print(f"    [green]PASSED:[/green]   {all_pass:>2d} / {all_total}")
    console.print(f"    [yellow]WARNINGS:[/yellow] {all_warn:>2d}")
    console.print(f"    [red]FAILED:[/red]   {all_fail:>2d}")

    # Phase failures
    for part_name, part_results in [("PHASE 3", part_a), ("PHASE 4", part_b), ("PHASE 5", part_c)]:
        failures = [r for r in part_results if r[0] == "FAIL"]
        if failures:
            console.print()
            console.print(f"  [bold red]{part_name} FAILURES:[/bold red]")
            for _, section, title, detail in failures:
                console.print(f"    [red]Section {section}: {title} -- {detail}[/red]")

    # Warnings
    all_warnings = [r for r in results if r[0] == "WARNING"]
    if all_warnings:
        console.print()
        console.print("  [bold yellow]WARNINGS:[/bold yellow]")
        for _, section, title, detail in all_warnings:
            console.print(f"    [yellow]Section {section}: {title} -- {detail}[/yellow]")

    # Production readiness
    console.print()
    if all_fail == 0 and all_warn <= 5:
        console.print("  [bold green]SYSTEM STATUS: PRODUCTION READY[/bold green]")
    elif all_fail <= 3:
        console.print("  [bold yellow]SYSTEM STATUS: NEEDS FIXES[/bold yellow]")
    else:
        console.print("  [bold red]SYSTEM STATUS: CRITICAL ISSUES[/bold red]")

    console.print()
    console.print(f"  [dim]Completed in {elapsed:.1f} seconds[/dim]")

    if all_fail > 0 or all_warn > 0:
        console.print()
        console.print("  [dim]NEXT STEPS:[/dim]")
        console.print("  [dim]Fix all failures, then investigate warnings.[/dim]")
        console.print("  [dim]Then run the manual checklist:[/dim]")
        console.print("  [dim]  python scripts/verify_phase5.py --manual-checklist[/dim]")


# ══════════════════════════════════════════════════════════════════
# MANUAL CHECKLIST
# ══════════════════════════════════════════════════════════════════

def print_manual_checklist() -> None:
    """Print the Phase 5 manual checklist from docs/aegis_phase5_manual_checklist.md."""
    console.print()
    console.rule("[bold]PHASE 5 MANUAL VERIFICATION CHECKLIST[/bold]")
    console.print()

    checklist_path = os.path.join("docs", "aegis_phase5_manual_checklist.md")
    if os.path.exists(checklist_path):
        try:
            with open(checklist_path, "r") as f:
                content = f.read()
            console.print(content)
        except Exception as e:
            console.print(f"  [red]Error reading checklist: {e}[/red]")
    else:
        console.print(f"  [yellow]Checklist file not found: {checklist_path}[/yellow]")
        console.print()
        console.print("  Open http://localhost:8000 and verify the following:")
        console.print()

        console.print(Panel.fit(
            "[bold]ADMIN SETTINGS PAGE (/admin)[/bold]\n"
            "  [ ] Page loads with collapsible sections for all categories\n"
            "  [ ] Settings show current values from .env defaults\n"
            "  [ ] Settings can be edited and saved (HTMX auto-save)\n"
            "  [ ] Changes take effect at runtime (e.g., polling intervals)\n"
            "  [ ] Voice profile section shows auto-generated profile\n"
            "  [ ] Custom voice rules can be added and saved\n"
            "  [ ] Notification toggles work",
            border_style="dim",
        ))

        console.print(Panel.fit(
            "[bold]SEARCH PAGE (/search)[/bold]\n"
            "  [ ] Search page loads with input field\n"
            "  [ ] Keyword search returns results across all content types\n"
            "  [ ] Semantic search returns contextually relevant results\n"
            "  [ ] Results show source type badges and are clickable\n"
            "  [ ] Search completes in <2 seconds",
            border_style="dim",
        ))

        console.print(Panel.fit(
            "[bold]ERROR HANDLING & RETRY[/bold]\n"
            "  [ ] Stop Screenpipe, wait 1 min, restart -- Aegis recovers?\n"
            "  [ ] Restart Docker PostgreSQL -- Aegis reconnects?\n"
            "  [ ] Check graph_client.py for retry logic with Retry-After headers\n"
            "  [ ] Check for exponential backoff with jitter in retry code",
            border_style="dim",
        ))

        console.print(Panel.fit(
            "[bold]TOKEN SECURITY[/bold]\n"
            "  [ ] ls -la ~/.aegis/msal_token_cache.json shows -rw------- (chmod 600)\n"
            "  [ ] grep logs for 'sk-ant|sk-proj|Bearer|access_token' returns zero matches\n"
            "  [ ] .env is in .gitignore",
            border_style="dim",
        ))

        console.print(Panel.fit(
            "[bold]BACKUP & RETENTION[/bold]\n"
            "  [ ] Backup script creates valid pg_dump\n"
            "  [ ] Backup rotation works (max 30 files)\n"
            "  [ ] Data retention tiers implemented (hot 90d / warm 365d)",
            border_style="dim",
        ))

        console.print(Panel.fit(
            "[bold]STARTUP & LOGGING[/bold]\n"
            "  [ ] aegis start/stop commands work\n"
            "  [ ] LaunchAgent auto-starts on boot\n"
            "  [ ] Crash recovery resets stuck processing items on startup\n"
            "  [ ] Log rotation is configured\n"
            "  [ ] No PII in log output",
            border_style="dim",
        ))

        console.print(Panel.fit(
            "[bold]MOBILE RESPONSIVE AUDIT (375px viewport)[/bold]\n"
            "  [ ] Dashboard / Command Center stacks vertically\n"
            "  [ ] Meeting list scrolls or switches to cards\n"
            "  [ ] Emails page readable, filters accessible\n"
            "  [ ] Sidebar collapses to hamburger menu\n"
            "  [ ] RAG Chat input and messages fit mobile",
            border_style="dim",
        ))

    console.print()
    console.print("  [dim]Estimated time for manual checklist: ~45 minutes[/dim]")
    console.print()


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

async def main() -> None:
    start_time = time.time()

    now = datetime.now()
    settings = get_settings()

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(settings.aegis_timezone)
        now = datetime.now(tz)
        tz_label = settings.aegis_timezone
    except Exception:
        tz_label = "local"

    console.print()
    console.print(Panel.fit(
        f"[bold]AEGIS -- Full System Verification Report[/bold]\n"
        f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S')} {tz_label}\n"
        f"Phase 3 + Phase 4 + Phase 5",
        border_style="bright_blue",
    ))

    async with async_session_factory() as session:
        try:
            # ── PART A: Phase 3 Re-Checks ──
            console.print()
            console.rule("[bold bright_blue]PART A: PHASE 3 RE-CHECKS (Sections 1-14)[/bold bright_blue]")

            await check_service_health(session)
            await check_email_ingestion(session)
            await check_email_triage(session)
            await check_email_extraction(session)
            await check_thread_resolution(session)
            await check_teams_ingestion(session)
            await check_teams_triage(session)
            await check_chat_asks(session)
            await check_teams_membership(session)
            await check_people_health(session)
            await check_workstream_detection(session)
            await check_embeddings(session)
            await check_llm_costs(session)
            await check_integration(session)

            # ── PART B: Phase 4 Re-Checks ──
            console.print()
            console.rule("[bold bright_blue]PART B: PHASE 4 RE-CHECKS (Sections 15-28)[/bold bright_blue]")

            await check_scheduler_health(session)
            await check_briefings(session)
            await check_meeting_prep_timing(session)
            await check_morning_content(session)
            await check_voice_profile(session)
            await check_drafts(session)
            await check_response_workflow(session)
            await check_readiness(session)
            await check_sentiment(session)
            await check_rag_chat(session)
            await check_dashboard_cache(session)
            await check_notifications(session)
            await check_llm_costs_phase4(session)
            await check_end_to_end(session)

            # ── PART C: Phase 5 Checks ──
            console.print()
            console.rule("[bold bright_blue]PART C: PHASE 5 CHECKS (Sections 29-42)[/bold bright_blue]")

            await check_error_handling(session)
            await check_rate_limit_handling(session)
            await check_token_security(session)
            await check_pii_logging(session)
            await check_database_backup(session)
            await check_data_retention(session)
            await check_admin_settings(session)
            await check_admin_override(session)
            await check_crash_recovery(session)
            await check_startup_script(session)
            await check_logging(session)
            await check_search(session)
            await check_mobile_responsive(session)
            await check_docker_db_health(session)

        except Exception as ex:
            console.print(f"\n  [bold red]Error during verification: {ex}[/bold red]")
            import traceback
            traceback.print_exc()

    elapsed = time.time() - start_time
    print_summary(elapsed)
    console.print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aegis Phase 3+4+5 Full System Verification Script")
    parser.add_argument("--verbose", action="store_true", help="Show sample rows for each check")
    parser.add_argument("--fix-suggestions", action="store_true", help="Include suggested fixes for failures")
    parser.add_argument("--manual-checklist", action="store_true",
                        help="Print the Phase 5 manual testing checklist (no DB queries)")
    args = parser.parse_args()

    VERBOSE = args.verbose
    FIX_SUGGESTIONS = args.fix_suggestions

    if args.manual_checklist:
        print_manual_checklist()
    else:
        asyncio.run(main())
