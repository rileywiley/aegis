"""Email poller — fetch emails from Graph API, classify noise, upsert into DB."""

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import Attachment, Email, Person, SystemHealth
from aegis.db.repositories import get_or_create_person_by_email, upsert_system_health
from aegis.ingestion.graph_client import GraphClient

logger = logging.getLogger(__name__)

# ── Noise classification constants ─────────────────────────

_NOREPLY_PATTERNS = re.compile(
    r"(^|[.\-_])no[-_.]?reply|mailer[-_.]?daemon|postmaster@|bounce@",
    re.IGNORECASE,
)

_AUTOMATED_HEADER_VALUES = {
    "auto-generated",
    "auto-replied",
    "auto-notified",
}

_NEWSLETTER_DOMAINS = {
    "mailchimp.com",
    "sendgrid.net",
    "constantcontact.com",
    "campaign-archive.com",
    "hubspot.com",
    "marketo.com",
    "pardot.com",
    "mailgun.org",
    "sendinblue.com",
    "convertkit.com",
}

_UNSUBSCRIBE_PATTERN = re.compile(r"\bunsubscribe\b", re.IGNORECASE)


class EmailPoller:
    """Fetches new emails from Microsoft Graph and upserts them into the database."""

    def __init__(self, graph_client: GraphClient) -> None:
        self._graph = graph_client

    async def poll(self, session: AsyncSession) -> int:
        """Fetch new emails since last poll. Returns count of new emails stored."""
        try:
            count = await self._do_poll(session)
            await upsert_system_health(
                session,
                service="email_poller",
                status="healthy",
                last_success=datetime.now(timezone.utc),
                items_processed=count,
            )
            return count
        except Exception as exc:
            logger.exception("Email polling failed")
            await upsert_system_health(
                session,
                service="email_poller",
                status="degraded",
                last_error=datetime.now(timezone.utc),
                last_error_message=str(exc)[:500],
            )
            raise

    async def _do_poll(self, session: AsyncSession) -> int:
        last_seen = await self._get_last_seen(session)
        if not last_seen:
            # First run: only fetch last 7 days instead of entire inbox
            last_seen = datetime.now(timezone.utc) - timedelta(days=7)
        since_str = last_seen.isoformat()

        logger.info("Email poll: fetching messages since %s", since_str)
        messages = await self._graph.get_messages(folder="inbox", since=since_str)
        logger.info("Email poll: received %d messages from Graph API", len(messages))

        new_count = 0
        for msg in messages:
            stored = await self._upsert_email(session, msg)
            if stored:
                new_count += 1

        if new_count:
            await session.commit()

        logger.info("Email poll: stored %d new emails", new_count)
        return new_count

    async def _get_last_seen(self, session: AsyncSession) -> datetime | None:
        """Get the most recent email datetime in the database."""
        stmt = select(func.max(Email.datetime_))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _upsert_email(self, session: AsyncSession, msg: dict) -> bool:
        """Upsert a single email message. Returns True if new row was created."""
        graph_id = msg.get("id")
        if not graph_id:
            return False

        # Parse sender
        from_info = msg.get("from", {}).get("emailAddress", {})
        sender_email = from_info.get("address", "")
        sender_name = from_info.get("name", "")

        # Resolve sender to Person
        sender_id = None
        if sender_email:
            person = await get_or_create_person_by_email(
                session, email=sender_email, name=sender_name, source="email"
            )
            sender_id = person.id

        # Build recipients JSONB
        recipients = []
        for recip in msg.get("toRecipients", []):
            addr = recip.get("emailAddress", {})
            recipients.append({
                "email": addr.get("address", ""),
                "name": addr.get("name", ""),
                "type": "to",
            })
        for recip in msg.get("ccRecipients", []):
            addr = recip.get("emailAddress", {})
            recipients.append({
                "email": addr.get("address", ""),
                "name": addr.get("name", ""),
                "type": "cc",
            })

        # Parse datetime
        received_dt = _parse_graph_datetime(msg.get("receivedDateTime", ""))

        # Extract body text
        body = msg.get("body", {})
        body_text = body.get("content", "") if body.get("contentType") == "text" else ""
        # For HTML bodies, use bodyPreview as the text representation
        if not body_text:
            body_text = msg.get("bodyPreview", "")
        body_preview = msg.get("bodyPreview", "")

        # Classify noise
        headers = msg.get("internetMessageHeaders", [])
        email_class = classify_email_noise(
            sender_email=sender_email,
            body_text=body_text,
            body_preview=body_preview,
            headers=headers,
        )

        # Only human emails get pending processing status
        processing_status = "pending" if email_class == "human" else "completed"

        has_attachments = msg.get("hasAttachments", False)

        data = {
            "graph_id": graph_id,
            "subject": msg.get("subject"),
            "sender_id": sender_id,
            "recipients": recipients,
            "datetime": received_dt,
            "body_text": body_text,
            "body_preview": body_preview,
            "thread_id": msg.get("conversationId"),
            "is_read": msg.get("isRead"),
            "importance": msg.get("importance"),
            "has_attachments": has_attachments,
            "email_class": email_class,
            "processing_status": processing_status,
        }

        stmt = pg_insert(Email).values(**data)
        stmt = stmt.on_conflict_do_update(
            index_elements=["graph_id"],
            set_={
                "is_read": data["is_read"],
                "importance": data["importance"],
            },
        )
        stmt = stmt.returning(Email.__table__.c.id)
        result = await session.execute(stmt)
        email_id = result.scalar_one()

        # Store attachment metadata if present
        if has_attachments:
            await self._store_attachments(session, graph_id, email_id)

        return True

    async def _store_attachments(
        self, session: AsyncSession, graph_message_id: str, email_id: int
    ) -> None:
        """Fetch and store attachment metadata (no file content)."""
        try:
            attachments = await self._graph.get_message_attachments(graph_message_id)
        except Exception:
            logger.warning(
                "Failed to fetch attachments for message %s", graph_message_id[:20]
            )
            return

        for att in attachments:
            att_data = {
                "source_type": "email",
                "source_id": email_id,
                "graph_attachment_id": att.get("id"),
                "filename": att.get("name", "unknown"),
                "content_type": att.get("contentType"),
                "size_bytes": att.get("size"),
                "is_inline": att.get("isInline", False),
            }
            stmt = pg_insert(Attachment).values(**att_data)
            # Avoid duplicates by checking graph_attachment_id uniqueness manually
            # (no unique constraint on attachments, so just insert)
            await session.execute(stmt)


def classify_email_noise(
    sender_email: str,
    body_text: str,
    body_preview: str,
    headers: list[dict] | None = None,
) -> str:
    """Classify email as 'human', 'automated', or 'newsletter'.

    Rule-based classification (no LLM cost). This is the first filter
    before triage — only 'human' emails enter the triage batch.
    """
    headers = headers or []

    # Check for automated headers (X-Auto-Response, Auto-Submitted, etc.)
    for header in headers:
        header_name = (header.get("name", "") or "").lower()
        header_value = (header.get("value", "") or "").lower()
        if header_name in ("x-auto-response-suppress", "auto-submitted"):
            if header_value and header_value not in ("no",):
                return "automated"
        if header_name == "x-mailer" and "auto" in header_value:
            return "automated"

    # Check sender address patterns
    if _NOREPLY_PATTERNS.search(sender_email):
        return "automated"

    # Check sender domain for known marketing platforms
    domain = sender_email.split("@")[-1].lower() if "@" in sender_email else ""
    if domain in _NEWSLETTER_DOMAINS:
        return "newsletter"

    # Check for unsubscribe links in body
    text_to_check = body_text or body_preview or ""
    if _UNSUBSCRIBE_PATTERN.search(text_to_check):
        return "newsletter"

    # Check for List-Unsubscribe header
    for header in headers:
        if (header.get("name", "") or "").lower() == "list-unsubscribe":
            return "newsletter"

    return "human"


def _parse_graph_datetime(dt_str: str) -> datetime:
    """Parse a Graph API datetime string to UTC datetime."""
    if not dt_str:
        return datetime.now(timezone.utc)

    # Remove trailing Z and parse
    dt_str = dt_str.rstrip("Z")
    try:
        dt = datetime.fromisoformat(dt_str)
    except ValueError:
        return datetime.now(timezone.utc)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
