"""Nav badge counts — cached counts for sidebar badges."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# In-memory cache refreshed every 60 seconds
_cache: dict[str, int] = {}
_last_refresh: datetime | None = None
_TTL_SECONDS = 60


async def get_nav_counts() -> dict[str, int]:
    """Return cached nav counts. Refreshes from DB if stale."""
    global _cache, _last_refresh

    now = datetime.now(timezone.utc)
    if _last_refresh and (now - _last_refresh).total_seconds() < _TTL_SECONDS:
        return _cache

    try:
        from sqlalchemy import func, select
        from aegis.db.engine import async_session_factory
        from aegis.db.models import ActionItem, ChatAsk, EmailAsk, Person

        async with async_session_factory() as session:
            # Open actions
            actions_result = await session.execute(
                select(func.count()).select_from(ActionItem).where(
                    ActionItem.status.in_(["open", "in_progress"])
                )
            )
            actions_count = actions_result.scalar_one()

            # Open asks (email + chat)
            ea_result = await session.execute(
                select(func.count()).select_from(EmailAsk).where(
                    EmailAsk.status.in_(["open", "in_progress"])
                )
            )
            ca_result = await session.execute(
                select(func.count()).select_from(ChatAsk).where(
                    ChatAsk.status.in_(["open", "in_progress"])
                )
            )
            asks_count = ea_result.scalar_one() + ca_result.scalar_one()

            # People needing review (internal only)
            people_result = await session.execute(
                select(func.count()).select_from(Person).where(
                    Person.needs_review.is_(True),
                    Person.is_external.is_(False),
                )
            )
            people_count = people_result.scalar_one()

            _cache = {
                "/actions": actions_count,
                "/asks": asks_count,
                "/people": people_count,
            }
            _last_refresh = now

    except Exception:
        logger.debug("Failed to refresh nav counts", exc_info=True)

    return _cache
