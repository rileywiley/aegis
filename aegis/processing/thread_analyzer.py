"""Thread analysis — determine which email asks are resolved vs still pending."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import Email, EmailAsk

logger = logging.getLogger(__name__)


async def analyze_thread(session: AsyncSession, thread_id: str) -> None:
    """Analyze an email thread to resolve completed asks.

    For each open EmailAsk in the thread, check if the target person
    replied after the ask was created. If so, mark the ask as completed.
    """
    if not thread_id:
        return

    # Fetch all emails in this thread, ordered by datetime
    emails_stmt = (
        select(Email)
        .where(Email.thread_id == thread_id)
        .order_by(Email.datetime_.asc())
    )
    result = await session.execute(emails_stmt)
    thread_emails = list(result.scalars().all())

    if len(thread_emails) < 2:
        # Need at least 2 emails for thread resolution
        return

    # Build a set of (sender_id, datetime) for quick lookups
    replies: list[tuple[int | None, datetime]] = [
        (e.sender_id, e.datetime_) for e in thread_emails if e.sender_id
    ]

    # Fetch all open asks in this thread
    asks_stmt = (
        select(EmailAsk)
        .where(EmailAsk.thread_id == thread_id)
        .where(EmailAsk.status == "open")
        .order_by(EmailAsk.created.asc())
    )
    asks_result = await session.execute(asks_stmt)
    open_asks = list(asks_result.scalars().all())

    if not open_asks:
        return

    resolved_count = 0

    for ask in open_asks:
        if not ask.target_id:
            continue

        # Check if the target person sent a reply AFTER the ask was created
        ask_created = ask.created
        resolving_email_id = None

        for email in thread_emails:
            if (
                email.sender_id == ask.target_id
                and email.datetime_ > ask_created
            ):
                resolving_email_id = email.id
                break

        if resolving_email_id:
            await session.execute(
                update(EmailAsk)
                .where(EmailAsk.id == ask.id)
                .values(
                    status="completed",
                    resolved_by_email_id=resolving_email_id,
                    updated=datetime.now(timezone.utc),
                )
            )
            resolved_count += 1
            logger.debug(
                "Resolved ask %d (thread=%s) — target replied in email %d",
                ask.id,
                thread_id[:20],
                resolving_email_id,
            )

    if resolved_count:
        await session.commit()
        logger.info(
            "Thread %s: resolved %d of %d open asks",
            thread_id[:20],
            resolved_count,
            len(open_asks),
        )
