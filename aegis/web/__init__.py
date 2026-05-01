"""Web layer — shared Jinja2 templates instance."""

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="aegis/web/templates")


def _get_nav_counts() -> dict[str, int]:
    """Synchronous accessor for cached nav counts (used in Jinja2 globals)."""
    from aegis.web.nav_counts import _cache
    return _cache


# Make nav_counts available to all templates
templates.env.globals["nav_counts"] = _get_nav_counts
