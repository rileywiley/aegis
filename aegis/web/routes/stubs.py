"""Stub routes for pages built in future phases — prevents 404s."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from aegis.config import get_settings
from aegis.web import templates

router = APIRouter()
settings = get_settings()


def _current_time() -> str:
    tz = ZoneInfo(settings.aegis_timezone)
    return datetime.now(tz).strftime("%-I:%M %p %Z")


_STUB_PAGES = {
    "/readiness": ("readiness.html", "Readiness"),
    "/ask": ("chat.html", "Ask Aegis"),
    "/admin": ("admin.html", "Admin"),
    "/search": ("search.html", "Search"),
    "/respond": ("respond.html", "Respond"),
}


# Register each stub route
for path, (template, title) in _STUB_PAGES.items():

    def _make_handler(tpl: str, page_title: str, has_path_param: bool = False):
        if has_path_param:
            async def handler(request: Request, workstream_id: int = 0):
                return templates.TemplateResponse(
                    request,
                    tpl,
                    {
                        "current_time": _current_time(),
                        "page_title": page_title,
                    },
                )
        else:
            async def handler(request: Request):
                return templates.TemplateResponse(
                    request,
                    tpl,
                    {
                        "current_time": _current_time(),
                        "page_title": page_title,
                    },
                )
        handler.__name__ = f"stub_{tpl.replace('.html', '')}"
        return handler

    has_param = "{" in path
    router.add_api_route(path, _make_handler(template, title, has_param), methods=["GET"])
