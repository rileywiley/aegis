"""Teams ingestion — sync teams/channels/memberships, poll chat + channel messages."""

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import (
    Attachment,
    ChatMessage,
    Meeting,
    Team,
    TeamChannel,
    TeamMembership,
)
from aegis.db.repositories import get_or_create_person_by_email, upsert_system_health
from aegis.ingestion.graph_client import GraphClient

logger = logging.getLogger(__name__)

# Regex: message body contains ONLY emoji (Unicode emoji ranges) and whitespace
_EMOJI_ONLY_RE = re.compile(
    r"^[\s"
    r"\U0001F600-\U0001F64F"  # emoticons
    r"\U0001F300-\U0001F5FF"  # symbols & pictographs
    r"\U0001F680-\U0001F6FF"  # transport & map
    r"\U0001F1E0-\U0001F1FF"  # flags
    r"\U00002702-\U000027B0"  # dingbats
    r"\U000024C2-\U0001F251"  # enclosed chars
    r"\U0001F900-\U0001F9FF"  # supplemental symbols
    r"\U0001FA00-\U0001FA6F"  # chess symbols
    r"\U0001FA70-\U0001FAFF"  # symbols extended-A
    r"\U00002600-\U000026FF"  # misc symbols
    r"\U0000FE00-\U0000FE0F"  # variation selectors
    r"\U0000200D"             # zero width joiner
    r"\U0000200B-\U0000200F"  # zero width chars
    r"]+$"
)

# Track last-seen timestamps per chat/channel to support incremental polling
_last_seen: dict[str, str] = {}


def is_noise_message(msg: dict, min_length: int) -> bool:
    """Apply rule-based noise filter to a Teams message.

    Returns True if the message should be marked as noise.
    """
    # Skip system messages (only process messageType == 'message')
    if msg.get("messageType", "") != "message":
        return True

    body = msg.get("body", {})
    content = body.get("content", "")

    # Strip HTML tags for length/content checks
    plain_text = re.sub(r"<[^>]+>", "", content).strip()

    # Skip empty messages
    if not plain_text:
        return True

    # Skip messages shorter than minimum length
    if len(plain_text) < min_length:
        return True

    # Skip emoji-only messages
    if _EMOJI_ONLY_RE.match(plain_text):
        return True

    # Skip reaction-only messages (reactions field present but no real content,
    # or the body is a reaction system message like "<systemEventMessage/>")
    if "<systemEventMessage" in content:
        return True

    return False


def _extract_plain_text(body: dict) -> str:
    """Extract plain text from a Graph API message body."""
    content = body.get("content", "")
    content_type = body.get("contentType", "text")
    if content_type == "html":
        return re.sub(r"<[^>]+>", "", content).strip()
    return content.strip()


def _parse_datetime(dt_str: str | None) -> datetime:
    """Parse an ISO datetime string from Graph API, default to now(UTC)."""
    if not dt_str:
        return datetime.now(timezone.utc)
    # Graph returns ISO 8601 — handle both Z and +00:00 formats
    cleaned = dt_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


class TeamsPoller:
    """Polls Microsoft Teams for new messages from chats and channels."""

    def __init__(self, graph_client: GraphClient) -> None:
        self._graph = graph_client

    async def poll(self, session: AsyncSession) -> int:
        """Run a full Teams polling cycle.

        Returns the number of new messages stored.
        """
        settings = get_settings()
        total_new = 0

        # Step 1: Sync teams/channels/memberships (lightweight, idempotent)
        await self._sync_teams_structure(session)

        # Step 2: Fetch new messages from 1:1 and group chats
        total_new += await self._poll_chats(session, settings)

        # Step 3: Fetch new messages from channels
        total_new += await self._poll_channels(session, settings)

        logger.info("Teams poll complete — %d new messages", total_new)
        return total_new

    # ── Teams structure sync ──────────────────────────────

    async def _sync_teams_structure(self, session: AsyncSession) -> None:
        """Sync teams, channels, and memberships from Graph API."""
        try:
            teams_data = await self._graph.get_joined_teams()
        except Exception:
            logger.exception("Failed to fetch joined teams")
            return

        for team_data in teams_data:
            graph_team_id = team_data.get("id", "")
            team_name = team_data.get("displayName", "Unknown Team")
            team_desc = team_data.get("description")

            # Upsert team
            stmt = pg_insert(Team).values(
                graph_team_id=graph_team_id,
                name=team_name,
                description=team_desc,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["graph_team_id"],
                set_={"name": team_name, "description": team_desc},
            )
            stmt = stmt.returning(Team.__table__.c.id)
            result = await session.execute(stmt)
            team_id = result.scalar_one()

            # Sync channels for this team
            try:
                channels = await self._graph.get_team_channels(graph_team_id)
                for ch in channels:
                    ch_stmt = pg_insert(TeamChannel).values(
                        graph_channel_id=ch.get("id", ""),
                        team_id=team_id,
                        name=ch.get("displayName", ""),
                        description=ch.get("description"),
                    )
                    ch_stmt = ch_stmt.on_conflict_do_update(
                        index_elements=["graph_channel_id"],
                        set_={
                            "name": ch.get("displayName", ""),
                            "description": ch.get("description"),
                        },
                    )
                    await session.execute(ch_stmt)
            except Exception:
                logger.exception("Failed to sync channels for team %s", team_name)

            # Sync members for this team
            try:
                members = await self._graph.get_team_members(graph_team_id)
                for member in members:
                    email = member.get("email") or member.get("userPrincipalName")
                    name = member.get("displayName", "")
                    if not email or not name:
                        continue
                    person = await get_or_create_person_by_email(
                        session, email=email, name=name, source="teams"
                    )
                    mem_stmt = pg_insert(TeamMembership).values(
                        team_id=team_id,
                        person_id=person.id,
                        role=member.get("roles", [None])[0] if member.get("roles") else None,
                    )
                    mem_stmt = mem_stmt.on_conflict_do_nothing(
                        index_elements=["team_id", "person_id"]
                    )
                    await session.execute(mem_stmt)
            except Exception:
                logger.exception("Failed to sync members for team %s", team_name)

        await session.commit()
        logger.info("Teams structure sync complete — %d teams", len(teams_data))

    # ── Chat polling ──────────────────────────────────────

    async def _poll_chats(self, session: AsyncSession, settings) -> int:
        """Poll 1:1 and group chats for new messages."""
        count = 0
        try:
            chats = await self._graph.get_chats()
        except Exception:
            logger.exception("Failed to fetch chats list")
            return 0

        for chat in chats:
            chat_id = chat.get("id", "")
            chat_type = chat.get("chatType", "")

            # Determine if this is a meeting chat
            online_meeting_id = chat.get("onlineMeetingId")

            since = _last_seen.get(f"chat:{chat_id}")
            if not since:
                # First run: only fetch last 7 days instead of entire chat history
                since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            try:
                messages = await self._graph.get_chat_messages(chat_id, since=since)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 403:
                    logger.debug("Skipping inaccessible chat %s (403 Forbidden)", chat_id)
                else:
                    logger.exception("Failed to fetch messages for chat %s", chat_id)
                continue
            except Exception:
                logger.exception("Failed to fetch messages for chat %s", chat_id)
                continue

            for msg in messages:
                stored = await self._store_message(
                    session,
                    msg=msg,
                    source_type="teams_chat",
                    chat_id=chat_id,
                    channel_id=None,
                    online_meeting_id=online_meeting_id,
                    settings=settings,
                )
                if stored:
                    count += 1

            # Update last-seen for this chat
            if messages:
                latest_dt = max(
                    m.get("createdDateTime", "") for m in messages
                )
                if latest_dt:
                    _last_seen[f"chat:{chat_id}"] = latest_dt

        await session.commit()
        return count

    # ── Channel polling ───────────────────────────────────

    async def _poll_channels(self, session: AsyncSession, settings) -> int:
        """Poll all channels for new messages."""
        count = 0

        # Get all teams + channels from DB
        teams_result = await session.execute(select(Team))
        teams = list(teams_result.scalars().all())

        for team in teams:
            channels_result = await session.execute(
                select(TeamChannel).where(TeamChannel.team_id == team.id)
            )
            channels = list(channels_result.scalars().all())

            for channel in channels:
                since = _last_seen.get(f"channel:{channel.graph_channel_id}")
                if not since:
                    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                try:
                    messages = await self._graph.get_channel_messages(
                        team.graph_team_id,
                        channel.graph_channel_id,
                        since=since,
                    )
                except httpx.ReadTimeout:
                    logger.warning(
                        "Timeout fetching messages for channel %s/%s — skipping this cycle",
                        team.name,
                        channel.name,
                    )
                    continue
                except Exception:
                    logger.exception(
                        "Failed to fetch messages for channel %s/%s",
                        team.name,
                        channel.name,
                    )
                    continue

                for msg in messages:
                    stored = await self._store_message(
                        session,
                        msg=msg,
                        source_type="teams_channel",
                        chat_id=None,
                        channel_id=channel.id,
                        online_meeting_id=None,
                        settings=settings,
                    )
                    if stored:
                        count += 1

                # Update last-seen
                if messages:
                    latest_dt = max(
                        m.get("createdDateTime", "") for m in messages
                    )
                    if latest_dt:
                        _last_seen[f"channel:{channel.graph_channel_id}"] = latest_dt

        await session.commit()
        return count

    # ── Message storage ───────────────────────────────────

    async def _store_message(
        self,
        session: AsyncSession,
        msg: dict,
        source_type: str,
        chat_id: str | None,
        channel_id: int | None,
        online_meeting_id: str | None,
        settings,
    ) -> bool:
        """Store a single Teams message. Returns True if a new row was inserted."""
        graph_message_id = msg.get("id", "")
        if not graph_message_id:
            return False

        # Check if already stored
        existing = await session.execute(
            select(ChatMessage.id).where(
                ChatMessage.graph_message_id == graph_message_id
            )
        )
        if existing.scalar_one_or_none() is not None:
            return False

        body = msg.get("body", {})
        plain_text = _extract_plain_text(body)
        msg_datetime = _parse_datetime(msg.get("createdDateTime"))

        # Noise filter
        noise = is_noise_message(msg, settings.teams_min_message_length)

        # Resolve sender — try email first, fall back to display name match
        sender_id = None
        sender = msg.get("from", {})
        user_info = sender.get("user", {}) if sender else {}
        sender_email = user_info.get("email") or user_info.get("userPrincipalName")
        sender_name = user_info.get("displayName", "")
        if sender_email:
            person = await get_or_create_person_by_email(
                session, email=sender_email, name=sender_name or sender_email, source="teams"
            )
            sender_id = person.id
        elif sender_name:
            # No email — try fuzzy match by display name against existing people
            from rapidfuzz import fuzz
            from aegis.db.models import Person
            stmt = select(Person).where(Person.name.ilike(f"%{sender_name[:20]}%")).limit(5)
            matches = (await session.execute(stmt)).scalars().all()
            best = None
            for p in matches:
                if fuzz.ratio(sender_name.lower(), p.name.lower()) >= 80:
                    best = p
                    break
            if best:
                sender_id = best.id
            else:
                # Create stub person without email
                person = Person(name=sender_name, source="teams", needs_review=True, confidence=0.3)
                session.add(person)
                await session.flush()
                sender_id = person.id

        # Link meeting chats
        linked_meeting_id = None
        if online_meeting_id:
            meeting_result = await session.execute(
                select(Meeting.id).where(
                    Meeting.calendar_event_id == online_meeting_id
                )
            )
            linked_meeting_id = meeting_result.scalar_one_or_none()

        # Thread root
        thread_root_id = None
        # Channel messages may have a replyToId for threading
        reply_to = msg.get("replyToId")
        if reply_to:
            thread_root_id = reply_to

        chat_msg = ChatMessage(
            graph_message_id=graph_message_id,
            source_type=source_type,
            chat_id=chat_id,
            channel_id=channel_id,
            sender_id=sender_id,
            datetime_=msg_datetime,
            body_text=plain_text,
            body_preview=plain_text[:150] if plain_text else None,
            thread_root_id=thread_root_id,
            linked_meeting_id=linked_meeting_id,
            noise_filtered=noise,
            processing_status="pending" if not noise else "completed",
        )
        session.add(chat_msg)
        await session.flush()

        # Store attachment metadata (skip non-file attachments like messageReference)
        attachments = msg.get("attachments", [])
        for att in attachments:
            filename = att.get("name") or att.get("fileName")
            content_type = att.get("contentType", "")
            # Skip non-file attachments (message references, adaptive cards, etc.)
            if not filename or content_type in ("messageReference", "application/vnd.microsoft.card.adaptive"):
                continue
            att_record = Attachment(
                source_type="chat_message",
                source_id=chat_msg.id,
                graph_attachment_id=att.get("id"),
                filename=filename,
                content_type=content_type,
                size_bytes=None,
                is_inline=False,
            )
            session.add(att_record)

        return True
