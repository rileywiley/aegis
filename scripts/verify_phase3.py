#!/usr/bin/env python3
"""Aegis Phase 3 Verification Script — read-only diagnostic against the live database."""

import argparse
import asyncio
import sys
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
    settings = get_settings()

    # Try common user email patterns from config or well-known addresses
    candidate_emails: list[str] = []

    # The user's email from CLAUDE.md context
    candidate_emails.append("delemos.ricardo@gmail.com")

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
# SECTION 1: SERVICE HEALTH
# ══════════════════════════════════════════════════════════════════

async def check_service_health(session) -> None:
    section_header(1, "SERVICE HEALTH")

    row = await session.execute(text("SELECT COUNT(*) FROM system_health"))
    count = row.scalar()
    if count == 0:
        console.print("  [red]No services registered in system_health table[/red]")
        record_result("FAIL", 1, "Service Health", "system_health table is empty")
        suggest("Ensure all pollers call update_system_health() after each cycle.")
        return

    rows = await session.execute(
        text("""
            SELECT service, status, last_success, last_error, last_error_message,
                   items_processed_last_hour, updated
            FROM system_health ORDER BY service
        """)
    )
    services = rows.fetchall()

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

        # Show error if recent (within 1 hour)
        error_msg = ""
        if svc.last_error:
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            le = svc.last_error
            if le.tzinfo is None:
                from zoneinfo import ZoneInfo
                le = le.replace(tzinfo=ZoneInfo("UTC"))
            if (now - le).total_seconds() < 3600 and svc.last_error_message:
                error_msg = svc.last_error_message[:40]

        table.add_row(
            icon,
            svc.service,
            svc.status or "unknown",
            time_ago(svc.last_success),
            str(svc.items_processed_last_hour or 0),
            error_msg,
        )

    console.print(table)

    if has_down:
        record_result("FAIL", 1, "Service Health", "One or more services are down")
        suggest("Check logs for the down service. Restart the relevant poller.")
    elif has_degraded:
        record_result("WARNING", 1, "Service Health", "One or more services degraded")
        suggest("Service may recover on its own. Check last_error_message for details.")
    else:
        record_result("PASS", 1, "Service Health", "All services healthy")


# ══════════════════════════════════════════════════════════════════
# SECTION 2: EMAIL INGESTION
# ══════════════════════════════════════════════════════════════════

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

    if distinct_classes >= 3 and human_count > 0:
        console.print("  [green]Noise filter active -- classification distribution looks healthy[/green]")
        record_result("PASS", 2, "Email Ingestion", f"{total} emails, {distinct_classes} classes")
    elif distinct_classes == 0 or human_count == 0:
        console.print("  [yellow]Missing email_class values or zero human emails[/yellow]")
        record_result("WARNING", 2, "Email Ingestion", "One class has 0 count or human < 10%")
        suggest("Check email_poller.py noise classification logic.")
    else:
        if human_count < total * 0.1:
            record_result("WARNING", 2, "Email Ingestion", f"Human emails only {human_count / total * 100:.1f}% of total")
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


# ══════════════════════════════════════════════════════════════════
# SECTION 3: EMAIL TRIAGE
# ══════════════════════════════════════════════════════════════════

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
    elif sub_pct > 60:
        record_result("WARNING", 3, "Email Triage", f"Substantive too high: {sub_pct:.0f}% (threshold too low?)")
        suggest("Increase triage_substantive_threshold in config.")
    elif sub_pct < 15:
        record_result("WARNING", 3, "Email Triage", f"Substantive too low: {sub_pct:.0f}% (threshold too high?)")
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


# ══════════════════════════════════════════════════════════════════
# SECTION 4: EMAIL EXTRACTION & ASK DIRECTIONALITY
# ══════════════════════════════════════════════════════════════════

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

    if has_both_pct >= 50:
        status = "PASS"
        detail = f"{total_asks} asks, {has_both_pct:.0f}% have full directionality"
    elif total_asks > 0:
        status = "WARNING"
        detail = f"Only {has_both_pct:.0f}% of asks have both requester and target"
        suggest("Check resolver.py -- entity resolution may not be linking people to asks.")
    else:
        status = "FAIL"
        detail = "No asks with requester/target"

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
        suggest("Add user's email to people table or check entity resolution.")

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


# ══════════════════════════════════════════════════════════════════
# SECTION 5: EMAIL THREAD RESOLUTION
# ══════════════════════════════════════════════════════════════════

async def check_thread_resolution(session) -> None:
    section_header(5, "EMAIL THREAD RESOLUTION")

    row = await session.execute(
        text("""
            SELECT COUNT(DISTINCT thread_id) as total_threads,
                   COUNT(DISTINCT thread_id) FILTER (
                       WHERE thread_id IN (
                           SELECT thread_id FROM emails
                           WHERE thread_id IS NOT NULL
                           GROUP BY thread_id HAVING COUNT(*) > 1
                       )
                   ) as multi_email_threads
            FROM emails WHERE thread_id IS NOT NULL
        """)
    )
    r = row.first()
    total_threads = r.total_threads
    multi_threads = r.multi_email_threads

    console.print(f"  Total threads: [bold]{total_threads}[/bold]")
    console.print(f"  Multi-email threads: [bold]{multi_threads}[/bold]")

    if multi_threads == 0:
        console.print("  [red]No multi-email threads found (thread_id not being set)[/red]")
        record_result("FAIL", 5, "Thread Resolution", "No multi-email threads found")
        suggest("Check email_poller.py -- ensure thread_id (conversationId) is being stored from Graph API.")
        return

    row2 = await session.execute(
        text("""
            SELECT COUNT(*) FILTER (WHERE status = 'completed' AND resolved_by_email_id IS NOT NULL) as resolved,
                   COUNT(*) FILTER (WHERE status = 'open') as still_open,
                   COUNT(*) as total
            FROM email_asks
        """)
    )
    r2 = row2.first()
    console.print(f"  Asks resolved by later email: [bold]{r2.resolved}[/bold]")
    console.print(f"  Asks still open: [bold]{r2.still_open}[/bold]")

    if r2.resolved and r2.resolved > 0:
        record_result("PASS", 5, "Thread Resolution", f"{r2.resolved} asks resolved via thread analysis")
    elif multi_threads > 0:
        record_result("WARNING", 5, "Thread Resolution",
                       "Multi-email threads exist but zero asks resolved by thread analysis")
        suggest("Check processing/thread_analyzer.py -- thread resolution may not be running.")
    else:
        record_result("WARNING", 5, "Thread Resolution", "Cannot assess thread resolution")

    if VERBOSE and multi_threads > 0:
        console.print()
        console.print("  [dim]Sample multi-email thread:[/dim]")
        # Pick a thread with multiple emails
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


# ══════════════════════════════════════════════════════════════════
# SECTION 6: TEAMS INGESTION
# ══════════════════════════════════════════════════════════════════

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
    any_filtered = False
    for t in types:
        source_types_found.add(t.source_type)
        if t.filtered and t.filtered > 0:
            any_filtered = True
        table.add_row(t.source_type, str(t.total), str(t.filtered or 0), str(t.kept or 0))

    console.print(table)

    has_chat = "teams_chat" in source_types_found
    has_channel = "teams_channel" in source_types_found

    if has_chat and has_channel and any_filtered:
        record_result("PASS", 6, "Teams Ingestion", f"{total} messages across both source types")
    elif not has_chat or not has_channel:
        missing = []
        if not has_chat:
            missing.append("teams_chat")
        if not has_channel:
            missing.append("teams_channel")
        record_result("WARNING", 6, "Teams Ingestion", f"Missing source type(s): {', '.join(missing)}")
        suggest("Check teams_poller.py -- may not be polling both chats and channels.")
    elif not any_filtered:
        record_result("WARNING", 6, "Teams Ingestion", "No messages noise-filtered (filter may not be working)")
        suggest("Check teams_poller noise_filter logic.")
    else:
        record_result("PASS", 6, "Teams Ingestion", f"{total} messages")

    if VERBOSE:
        console.print()
        console.print("  [dim]Sample filtered messages (should be noise):[/dim]")
        rows = await session.execute(
            text("SELECT body_preview FROM chat_messages WHERE noise_filtered = true LIMIT 5")
        )
        for r in rows.fetchall():
            console.print(f"    [dim]- {(r.body_preview or '(empty)')[:70]}[/dim]")

        console.print("  [dim]Sample kept messages (should be substantive):[/dim]")
        rows = await session.execute(
            text("SELECT body_preview FROM chat_messages WHERE noise_filtered = false LIMIT 5")
        )
        for r in rows.fetchall():
            console.print(f"    [dim]- {(r.body_preview or '(empty)')[:70]}[/dim]")


# ══════════════════════════════════════════════════════════════════
# SECTION 7: TEAMS TRIAGE
# ══════════════════════════════════════════════════════════════════

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

    non_null_classes = [k for k in triage_dict if k is not None]
    null_count = triage_dict.get(None, 0)

    if len(non_null_classes) > 0 and null_count < non_filtered:
        record_result("PASS", 7, "Teams Triage", f"{len(non_null_classes)} triage classes present")
    elif null_count == non_filtered:
        console.print("  [yellow]All non-filtered messages have NULL triage_class[/yellow]")
        record_result("WARNING", 7, "Teams Triage", "Triage not running on chat messages")
        suggest("Check processing/triage.py -- ensure chat_messages are included in triage batch.")
    else:
        record_result("PASS", 7, "Teams Triage", "Triage classes present")


# ══════════════════════════════════════════════════════════════════
# SECTION 8: CHAT ASKS EXTRACTION
# ══════════════════════════════════════════════════════════════════

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
        # Check if there are substantive chat messages (if so, extraction might be broken)
        row2 = await session.execute(
            text("SELECT COUNT(*) FROM chat_messages WHERE triage_class = 'substantive'")
        )
        substantive_count = row2.scalar()

        if substantive_count and substantive_count > 0:
            console.print(f"  [yellow]{substantive_count} substantive chat messages exist but no asks extracted[/yellow]")
            record_result("WARNING", 8, "Chat Asks", "Substantive chats exist but no asks extracted")
            suggest("Check processing/chat_extractor.py -- extraction may not be producing asks.")
        else:
            console.print("  [yellow]No substantive chat messages exist -- chat asks table being empty may be expected[/yellow]")
            record_result("WARNING", 8, "Chat Asks", "No substantive chats, so empty asks table may be expected")
        return

    console.print(f"    With requester: {r.has_requester}")
    console.print(f"    With target:    {r.has_target}")

    if r.has_requester > 0 and r.has_target > 0:
        record_result("PASS", 8, "Chat Asks", f"{r.total} asks with directionality")
    else:
        record_result("WARNING", 8, "Chat Asks", f"{r.total} asks but missing directionality")
        suggest("Check chat_extractor.py and resolver.py for directionality assignment.")


# ══════════════════════════════════════════════════════════════════
# SECTION 9: TEAMS MEMBERSHIP & ORG STRUCTURE
# ══════════════════════════════════════════════════════════════════

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
        console.print("  [red]Teams table is empty -- Teams polling not working[/red]")
        record_result("FAIL", 9, "Teams Membership", "Teams table is empty")
        suggest("Check ingestion/teams_poller.py and Team.ReadBasic.All permission.")
        return

    # Department inference
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
            dept_table.add_row(d.name, d.source or "?", f"{d.confidence:.2f}" if d.confidence else "?", str(d.member_count))
        console.print(dept_table)

    has_teams_dept = any(d.source == "teams" for d in depts) if depts else False

    if teams_count > 0 and channels_count > 0 and members_count > 0 and has_teams_dept:
        record_result("PASS", 9, "Teams Membership", f"{teams_count} teams, {depts and len(depts) or 0} depts")
    elif teams_count > 0 and not has_teams_dept:
        record_result("WARNING", 9, "Teams Membership",
                       "Teams exist but no departments inferred (weekly batch may not have run)")
        suggest("Run org_inference manually or wait for the weekly batch job.")
    else:
        record_result("PASS", 9, "Teams Membership", f"{teams_count} teams, {channels_count} channels")


# ══════════════════════════════════════════════════════════════════
# SECTION 10: PEOPLE TABLE HEALTH
# ══════════════════════════════════════════════════════════════════

async def check_people_health(session) -> None:
    section_header(10, "PEOPLE TABLE HEALTH")

    row = await session.execute(text("SELECT COUNT(*) FROM people"))
    total = row.scalar()

    if total == 0:
        console.print("  [red]People table is empty[/red]")
        record_result("FAIL", 10, "People Health", "People table is empty")
        suggest("Extraction/resolution not creating people. Check resolver.py.")
        return

    # By source
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

    # Department coverage
    row2 = await session.execute(
        text("""
            SELECT COUNT(*) FILTER (WHERE department_id IS NOT NULL) as has_dept,
                   COUNT(*) FILTER (WHERE department_id IS NULL) as no_dept,
                   COUNT(*) as total
            FROM people
        """)
    )
    r2 = row2.first()
    console.print(f"\n  With department: {r2.has_dept} / {r2.total}")

    # Duplicate detection
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

    multi_source = len(source_names - {None}) >= 2
    few_dups = len(dups) <= 10

    if multi_source and few_dups:
        record_result("PASS", 10, "People Health", f"{total} people from {len(source_names)} sources, {len(dups)} dups")
    elif len(dups) > 10:
        record_result("WARNING", 10, "People Health", f"{len(dups)} duplicate name groups detected")
        suggest("Review resolver.py entity resolution. May need tighter fuzzy matching.")
    elif not multi_source:
        record_result("WARNING", 10, "People Health", f"People only from sources: {source_names}")
        suggest("Email/Teams extraction should create people from 'email' and 'teams' sources.")
    else:
        record_result("PASS", 10, "People Health", f"{total} people")


# ══════════════════════════════════════════════════════════════════
# SECTION 11: WORKSTREAM AUTO-DETECTION
# ══════════════════════════════════════════════════════════════════

async def check_workstream_detection(session) -> None:
    section_header(11, "WORKSTREAM AUTO-DETECTION")

    # Workstreams by creation method
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

    # Auto-detected workstream details
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

    # Multi-workstream items
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

    # Unassigned items
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

    has_auto = len(auto_ws) > 0
    has_3plus = any(w.items >= 3 for w in auto_ws) if auto_ws else False

    if has_auto and has_3plus:
        record_result("PASS", 11, "Workstream Detection",
                       f"{len(auto_ws)} auto workstreams, multi-ws items: {multi_count}")
    elif has_auto and not has_3plus:
        record_result("WARNING", 11, "Workstream Detection",
                       "Auto workstreams exist but all have <3 items")
        suggest("Workstream detector may be creating too many small workstreams.")
    else:
        # Check if only manual workstreams exist
        has_any = any(True for w in ws_types)
        if has_any:
            record_result("WARNING", 11, "Workstream Detection",
                           "Only manual workstreams exist -- auto-detection not running")
            suggest("Check processing/workstream_detector.py and ensure the weekly batch is scheduled.")
        else:
            record_result("FAIL", 11, "Workstream Detection", "Zero auto-detected workstreams")

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


# ══════════════════════════════════════════════════════════════════
# SECTION 12: EMBEDDINGS & VECTOR SEARCH
# ══════════════════════════════════════════════════════════════════

async def check_embeddings(session) -> None:
    section_header(12, "EMBEDDINGS & VECTOR SEARCH")

    rows = await session.execute(
        text("""
            SELECT 'meetings' as type,
                   COUNT(*) FILTER (WHERE embedding IS NOT NULL) as has_embedding,
                   COUNT(*) as total
            FROM meetings
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

    # Vector search test
    vector_ok = False
    try:
        test_row = await session.execute(
            text("""
                SELECT e.subject,
                       1 - (e.embedding <=> (SELECT embedding FROM emails WHERE embedding IS NOT NULL LIMIT 1)) as similarity
                FROM emails e WHERE embedding IS NOT NULL
                ORDER BY e.embedding <=> (SELECT embedding FROM emails WHERE embedding IS NOT NULL LIMIT 1)
                LIMIT 5
            """)
        )
        vector_results = test_row.fetchall()
        if vector_results:
            console.print(f"\n  [green]pgvector similarity search: working ({len(vector_results)} results)[/green]")
            vector_ok = True
            if VERBOSE:
                for vr in vector_results:
                    console.print(f"    [dim]- [{vr.similarity:.4f}] {(vr.subject or '(no subject)')[:50]}[/dim]")
        else:
            console.print("\n  [yellow]pgvector search returned no results (no emails with embeddings)[/yellow]")
    except Exception as ex:
        console.print(f"\n  [red]pgvector similarity search failed: {ex}[/red]")
        suggest("Ensure pgvector extension is installed and HNSW indexes are created.")

    if overall_pct >= 90 and vector_ok:
        record_result("PASS", 12, "Embeddings", f"{overall_pct:.0f}% coverage, vector search working")
    elif overall_pct >= 50:
        record_result("WARNING", 12, "Embeddings", f"{overall_pct:.0f}% coverage (some embeddings missing)")
        suggest("Check processing/embeddings.py -- some items may have failed embedding generation.")
    elif total_items == 0:
        record_result("WARNING", 12, "Embeddings", "No items to embed yet")
    else:
        record_result("FAIL", 12, "Embeddings", f"Only {overall_pct:.0f}% embedding coverage")
        suggest("Embedding generation may be broken. Check processing/embeddings.py and OpenAI API key.")


# ══════════════════════════════════════════════════════════════════
# SECTION 13: LLM COST TRACKING
# ══════════════════════════════════════════════════════════════════

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

    # Cost calculation
    PRICING = {
        "haiku": (0.25 / 1_000_000, 1.25 / 1_000_000),        # input, output per token
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
            price = PRICING["haiku"]  # default

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

    if total_cost < 15:
        record_result("PASS", 13, "LLM Costs", f"${total_cost:.2f}/week")
    elif total_cost <= 30:
        record_result("WARNING", 13, "LLM Costs", f"${total_cost:.2f}/week (higher than expected)")
        suggest("Check if noise filter/triage is working -- may be over-extracting.")
    else:
        record_result("FAIL", 13, "LLM Costs", f"${total_cost:.2f}/week (cost is too high)")
        suggest("Urgent: check triage thresholds and noise filter. May be processing noise items.")


# ══════════════════════════════════════════════════════════════════
# SECTION 14: CROSS-SYSTEM INTEGRATION
# ══════════════════════════════════════════════════════════════════

async def check_integration(session) -> None:
    section_header(14, "CROSS-SYSTEM INTEGRATION")

    checks = []

    # Email -> People
    row = await session.execute(
        text("""
            SELECT COUNT(*) FILTER (WHERE sender_id IS NOT NULL) as resolved,
                   COUNT(*) FILTER (WHERE sender_id IS NULL) as unresolved,
                   COUNT(*) as total
            FROM emails WHERE email_class = 'human'
        """)
    )
    r = row.first()
    email_total = r.total
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
                   COUNT(*) FILTER (WHERE sender_id IS NULL) as unresolved,
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
        checks.append(("WARNING", "Ask->Action linking", "Zero links (may be expected)"))

    # Meeting chat correlation
    row4 = await session.execute(
        text("SELECT COUNT(*) FROM chat_messages WHERE linked_meeting_id IS NOT NULL")
    )
    meeting_chats = row4.scalar()
    console.print(f"  Meeting chat links: {meeting_chats}")
    if meeting_chats and meeting_chats > 0:
        checks.append(("PASS", "Meeting chat correlation", f"{meeting_chats} linked"))
    else:
        checks.append(("WARNING", "Meeting chat correlation", "Zero links (may be expected if no Teams meetings)"))

    # Determine overall section status
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
# SUMMARY SCORECARD
# ══════════════════════════════════════════════════════════════════

def print_summary() -> None:
    console.print()
    console.rule("[bold]PHASE 3 VERIFICATION SUMMARY[/bold]")
    console.print()

    passed = [r for r in results if r[0] == "PASS"]
    warnings = [r for r in results if r[0] == "WARNING"]
    failures = [r for r in results if r[0] == "FAIL"]
    total = len(results)

    console.print(f"  [green]PASSED:[/green]   {len(passed):>2d} / {total} checks")
    console.print(f"  [yellow]WARNINGS:[/yellow] {len(warnings):>2d} / {total} checks")
    console.print(f"  [red]FAILED:[/red]   {len(failures):>2d} / {total} checks")

    if failures:
        console.print()
        console.print("  [bold red]FAILURES (must fix before Phase 4):[/bold red]")
        for status, section, title, detail in failures:
            console.print(f"    [red]Section {section}: {title} -- {detail}[/red]")

    if warnings:
        console.print()
        console.print("  [bold yellow]WARNINGS (should investigate):[/bold yellow]")
        for status, section, title, detail in warnings:
            console.print(f"    [yellow]Section {section}: {title} -- {detail}[/yellow]")

    if not failures and not warnings:
        console.print()
        console.print("  [bold green]All Phase 3 checks passed! Ready for Phase 4.[/bold green]")
    elif failures:
        console.print()
        console.print(f"  [dim]RECOMMENDATION: Fix the {len(failures)} failure(s). "
                       f"Re-run: python scripts/verify_phase3.py[/dim]")
    else:
        console.print()
        console.print(f"  [dim]RECOMMENDATION: Investigate {len(warnings)} warning(s). "
                       f"Some may resolve after batch jobs run.[/dim]")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

async def main() -> None:
    now = datetime.now()
    settings = get_settings()

    # Try to detect local timezone
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(settings.aegis_timezone)
        now = datetime.now(tz)
        tz_label = settings.aegis_timezone
    except Exception:
        tz_label = "local"

    console.print()
    console.print(Panel.fit(
        f"[bold]AEGIS -- Phase 3 Verification Report[/bold]\n"
        f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S')} {tz_label}",
        border_style="bright_blue",
    ))

    async with async_session_factory() as session:
        try:
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
        except Exception as ex:
            console.print(f"\n  [bold red]Error during verification: {ex}[/bold red]")
            import traceback
            traceback.print_exc()

    print_summary()
    console.print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aegis Phase 3 Verification Script")
    parser.add_argument("--verbose", action="store_true", help="Show sample rows for each check")
    parser.add_argument("--fix-suggestions", action="store_true", help="Include suggested fixes for failures")
    args = parser.parse_args()

    VERBOSE = args.verbose
    FIX_SUGGESTIONS = args.fix_suggestions

    asyncio.run(main())
