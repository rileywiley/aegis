"""Web layer — shared Jinja2 templates instance."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from aegis.config import get_settings

templates = Jinja2Templates(directory="aegis/web/templates")


def _get_nav_counts() -> dict[str, int]:
    """Synchronous accessor for cached nav counts (used in Jinja2 globals)."""
    from aegis.web.nav_counts import _cache
    return _cache


def _local_dt(value, fmt: str = "%b %-d, %-I:%M %p") -> str:
    """Format an epoch-seconds float OR a datetime as a local-tz string.

    Returns "—" for None/empty/0. Falls back to UTC if AEGIS_TIMEZONE is unset.
    """
    if value in (None, "", 0, 0.0):
        return "—"
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        return str(value)
    tzname = getattr(get_settings(), "aegis_timezone", "UTC") or "UTC"
    try:
        local = dt.astimezone(ZoneInfo(tzname))
    except Exception:
        local = dt
    return local.strftime(fmt)


def _local_time(value) -> str:
    """Just the time portion, e.g. '10:32 AM'."""
    return _local_dt(value, "%-I:%M %p")


# Make nav_counts available to all templates
templates.env.globals["nav_counts"] = _get_nav_counts
templates.env.filters["local_dt"] = _local_dt
templates.env.filters["local_time"] = _local_time
