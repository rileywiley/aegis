"""Async Screenpipe REST API client for audio transcripts and screen OCR."""

import logging
from datetime import datetime

import httpx

from aegis.config import get_settings

logger = logging.getLogger(__name__)


class ScreenpipeClient:
    """Wrapper around the Screenpipe REST API running on localhost:3030."""

    def __init__(self, base_url: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.screenpipe_url).rstrip("/")

    async def health_check(self) -> bool:
        """GET /health — returns True if Screenpipe is responding."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/health")
                return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError):
            logger.warning("Screenpipe health check failed (not reachable)")
            return False

    async def get_audio(
        self, start: datetime, end: datetime
    ) -> list[dict]:
        """Query Screenpipe for audio transcription chunks within a time range.

        Returns a list of dicts with keys like:
          - content.text (transcribed speech)
          - content.timestamp
          - content.speaker (optional diarization)
        Falls back to empty list on connection errors.
        """
        params = {
            "content_type": "audio",
            "start_time": _fmt_ts(start),
            "end_time": _fmt_ts(end),
            "limit": 1000,
        }
        return await self._search(params)

    async def get_screen_ocr(
        self, start: datetime, end: datetime
    ) -> list[dict]:
        """Query Screenpipe for screen OCR frames within a time range.

        Returns a list of dicts with keys like:
          - content.text (OCR text)
          - content.app_name
          - content.timestamp
        Falls back to empty list on connection errors.
        """
        params = {
            "content_type": "ocr",
            "start_time": _fmt_ts(start),
            "end_time": _fmt_ts(end),
            "limit": 1000,
        }
        return await self._search(params)

    async def _search(self, params: dict) -> list[dict]:
        """Execute a search request against Screenpipe and return results."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(f"{self.base_url}/search", params=params)
                resp.raise_for_status()
                data = resp.json()
                # Screenpipe returns {"data": [...]} with search results
                return data.get("data", [])
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning("Screenpipe search failed (connection): %s", type(exc).__name__)
            return []
        except httpx.HTTPStatusError as exc:
            logger.warning("Screenpipe search HTTP error: %s", exc.response.status_code)
            return []
        except Exception:
            logger.exception("Unexpected error querying Screenpipe")
            return []


def _fmt_ts(dt: datetime) -> str:
    """Format datetime to ISO-8601 string for Screenpipe API."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")
