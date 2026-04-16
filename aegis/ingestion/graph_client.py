"""Microsoft Graph API client — MSAL device-code auth + async httpx requests."""

import asyncio
import json
import logging
import os
import stat
from pathlib import Path

import httpx
import msal

from aegis.config import get_settings

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# All scopes needed across phases (requested up-front so the token covers later work).
GRAPH_SCOPES = [
    "Calendars.Read",
    "User.Read",
    "Mail.Read",
    "Chat.Read",
    "ChannelMessage.Read.All",
    "Team.ReadBasic.All",
    "Channel.ReadBasic.All",
]

_TOKEN_CACHE_DIR = Path.home() / ".aegis"
_TOKEN_CACHE_FILE = _TOKEN_CACHE_DIR / "msal_token_cache.json"


def _ensure_cache_dir() -> None:
    """Create ~/.aegis/ with restricted permissions if it does not exist."""
    _TOKEN_CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)


def _load_msal_cache() -> msal.SerializableTokenCache:
    """Load the MSAL token cache from disk."""
    cache = msal.SerializableTokenCache()
    if _TOKEN_CACHE_FILE.exists():
        cache.deserialize(_TOKEN_CACHE_FILE.read_text())
    return cache


def _save_msal_cache(cache: msal.SerializableTokenCache) -> None:
    """Persist the MSAL token cache to disk with chmod 600."""
    _ensure_cache_dir()
    _TOKEN_CACHE_FILE.write_text(cache.serialize())
    os.chmod(_TOKEN_CACHE_FILE, stat.S_IRUSR | stat.S_IWUSR)


class GraphClient:
    """Async Microsoft Graph API client with automatic pagination and retry."""

    def __init__(self) -> None:
        settings = get_settings()
        self._cache = _load_msal_cache()
        self._app = msal.PublicClientApplication(
            client_id=settings.azure_client_id,
            authority=f"https://login.microsoftonline.com/{settings.azure_tenant_id}",
            token_cache=self._cache,
        )
        self._http = httpx.AsyncClient(timeout=30.0)

    # ── Token management ──────────────────────────────────

    def _get_account(self) -> dict | None:
        """Return the first cached account, if any."""
        accounts = self._app.get_accounts()
        return accounts[0] if accounts else None

    def acquire_token_silent(self) -> str | None:
        """Try silent token refresh. Returns access_token or None."""
        account = self._get_account()
        if not account:
            return None
        result = self._app.acquire_token_silent(GRAPH_SCOPES, account=account)
        if result and "access_token" in result:
            _save_msal_cache(self._cache)
            return result["access_token"]
        return None

    def acquire_token_device_code(self) -> str:
        """Interactive device-code flow. Blocks until user completes browser auth."""
        flow = self._app.initiate_device_flow(scopes=GRAPH_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device code flow initiation failed: {json.dumps(flow)}")
        # Print the message so the user knows what to do
        print(flow["message"])
        result = self._app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Device code auth failed: {result.get('error_description', result)}")
        _save_msal_cache(self._cache)
        return result["access_token"]

    def get_access_token(self) -> str:
        """Get a valid access token — silent first, device code as fallback."""
        token = self.acquire_token_silent()
        if token:
            return token
        logger.info("Silent token refresh failed; starting device code flow")
        return self.acquire_token_device_code()

    # ── HTTP request layer ────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """Make a single Graph API request with auth header and 429 retry."""
        token = self.get_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        for attempt in range(5):
            response = await self._http.request(
                method, url, headers=headers, params=params, json=json_body,
            )
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "5"))
                logger.warning(
                    "Graph API 429 — retrying after %d seconds (attempt %d)", retry_after, attempt + 1
                )
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()

        raise RuntimeError("Graph API request failed after 5 retries due to rate limiting")

    async def _get_paginated(
        self,
        url: str,
        *,
        params: dict | None = None,
    ) -> list[dict]:
        """GET with automatic @odata.nextLink pagination and 100ms pacing."""
        all_items: list[dict] = []
        current_url = url
        current_params = params

        while current_url:
            data = await self._request("GET", current_url, params=current_params)
            all_items.extend(data.get("value", []))

            next_link = data.get("@odata.nextLink")
            if next_link:
                # nextLink is a full URL with params embedded — don't pass extra params
                current_url = next_link
                current_params = None
                await asyncio.sleep(0.1)  # 100ms pacing between pages
            else:
                break

        return all_items

    # ── Public API methods ────────────────────────────────

    async def get_me(self) -> dict:
        """GET /me — current user profile."""
        return await self._request("GET", f"{GRAPH_BASE_URL}/me")

    async def get_calendar_events(
        self,
        start_datetime: str,
        end_datetime: str,
    ) -> list[dict]:
        """Fetch calendar events in a time window.

        Args:
            start_datetime: ISO 8601 UTC string, e.g. "2026-04-15T00:00:00Z"
            end_datetime: ISO 8601 UTC string, e.g. "2026-04-17T00:00:00Z"

        Returns:
            List of event dicts from Graph API (all pages).
        """
        url = f"{GRAPH_BASE_URL}/me/calendarView"
        params = {
            "startDateTime": start_datetime,
            "endDateTime": end_datetime,
            "$select": (
                "id,subject,start,end,isAllDay,isCancelled,"
                "isOnlineMeeting,onlineMeetingUrl,onlineMeeting,"
                "showAs,responseStatus,attendees,organizer,"
                "seriesMasterId,recurrence,type"
            ),
            "$orderby": "start/dateTime",
            "$top": "100",
        }
        return await self._get_paginated(url, params=params)

    async def close(self) -> None:
        """Shut down the underlying httpx client."""
        await self._http.aclose()
