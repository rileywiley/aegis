"""Microsoft Graph API client — MSAL device-code auth + async httpx requests."""

import asyncio
import json
import logging
import os
import random
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

# HTTP status codes that are safe to retry (transient server errors + rate limits)
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}

# Exponential backoff base delays (seconds) for retries 0-4
_BACKOFF_DELAYS = [1, 2, 4, 8, 16]
_MAX_RETRIES = 5
_MAX_JITTER = 1.0  # seconds of random jitter added to backoff


def _ensure_cache_dir() -> None:
    """Create ~/.aegis/ with restricted permissions if it does not exist."""
    _TOKEN_CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)


def _verify_token_cache_permissions() -> None:
    """Check that the token cache file has chmod 600. Fix and warn if not."""
    if not _TOKEN_CACHE_FILE.exists():
        return
    current_mode = stat.S_IMODE(os.stat(_TOKEN_CACHE_FILE).st_mode)
    expected_mode = stat.S_IRUSR | stat.S_IWUSR  # 0o600
    if current_mode != expected_mode:
        logger.warning(
            "Token cache %s has permissions %o — fixing to 600",
            _TOKEN_CACHE_FILE,
            current_mode,
        )
        os.chmod(_TOKEN_CACHE_FILE, expected_mode)


def _load_msal_cache() -> msal.SerializableTokenCache:
    """Load the MSAL token cache from disk."""
    _ensure_cache_dir()
    _verify_token_cache_permissions()
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
        """Make a single Graph API request with auth header and retry for transient errors.

        Retries on 429, 500, 502, 503, 529 with exponential backoff + jitter.
        For 429, respects the Retry-After header if present.
        """
        token = self.get_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        last_exc: httpx.HTTPStatusError | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                response = await self._http.request(
                    method, url, headers=headers, params=params, json=json_body,
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in _RETRYABLE_STATUS_CODES:
                    raise

                last_exc = exc

                # Determine wait time
                if status == 429:
                    retry_after_header = exc.response.headers.get("Retry-After")
                    if retry_after_header:
                        wait = float(retry_after_header)
                    else:
                        wait = _BACKOFF_DELAYS[attempt] + random.uniform(0, _MAX_JITTER)
                else:
                    wait = _BACKOFF_DELAYS[attempt] + random.uniform(0, _MAX_JITTER)

                logger.warning(
                    "Graph API %d on %s %s — retry %d/%d in %.1fs",
                    status,
                    method,
                    url.split("?")[0],  # strip query params from log
                    attempt + 1,
                    _MAX_RETRIES,
                    wait,
                )
                await asyncio.sleep(wait)

        # All retries exhausted — raise the last error
        raise last_exc  # type: ignore[misc]

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

    # ── Email methods ──────────────────────────────────────

    async def get_messages(
        self,
        folder: str = "inbox",
        since: str | None = None,
        top: int = 100,
    ) -> list[dict]:
        """Fetch emails from a mail folder. Supports incremental sync via `since`."""
        url = f"{GRAPH_BASE_URL}/me/mailFolders/{folder}/messages"
        params: dict = {
            "$select": (
                "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                "bodyPreview,body,conversationId,isRead,importance,"
                "hasAttachments,internetMessageHeaders"
            ),
            "$orderby": "receivedDateTime desc",
            "$top": str(top),
        }
        if since:
            params["$filter"] = f"receivedDateTime ge {since}"
        return await self._get_paginated(url, params=params)

    async def get_message(self, message_id: str) -> dict:
        """Fetch a single email message by ID."""
        url = f"{GRAPH_BASE_URL}/me/messages/{message_id}"
        return await self._request("GET", url)

    async def get_message_attachments(self, message_id: str) -> list[dict]:
        """Fetch attachment metadata for a message."""
        url = f"{GRAPH_BASE_URL}/me/messages/{message_id}/attachments"
        params = {"$select": "id,name,contentType,size,isInline"}
        return await self._get_paginated(url, params=params)

    async def send_mail(
        self,
        subject: str,
        body: str,
        to: list[str],
        cc: list[str] | None = None,
        reply_to_id: str | None = None,
    ) -> None:
        """Send an email via Graph API (Mail.Send permission)."""
        to_recipients = [{"emailAddress": {"address": addr}} for addr in to]
        cc_recipients = [{"emailAddress": {"address": addr}} for addr in (cc or [])]
        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": to_recipients,
        }
        if cc_recipients:
            message["ccRecipients"] = cc_recipients

        payload: dict = {"message": message, "saveToSentItems": True}
        if reply_to_id:
            # For replies, use the reply endpoint instead
            url = f"{GRAPH_BASE_URL}/me/messages/{reply_to_id}/reply"
            await self._request("POST", url, json={"comment": body})
            return

        url = f"{GRAPH_BASE_URL}/me/sendMail"
        await self._request("POST", url, json=payload)

    # ── Teams methods ────────────────────────────────────

    async def get_joined_teams(self) -> list[dict]:
        """GET /me/joinedTeams — list teams the user belongs to."""
        url = f"{GRAPH_BASE_URL}/me/joinedTeams"
        return await self._get_paginated(url)

    async def get_team_channels(self, team_id: str) -> list[dict]:
        """GET /teams/{id}/channels — list channels in a team."""
        url = f"{GRAPH_BASE_URL}/teams/{team_id}/channels"
        return await self._get_paginated(url)

    async def get_team_members(self, team_id: str) -> list[dict]:
        """GET /teams/{id}/members — list team members."""
        url = f"{GRAPH_BASE_URL}/teams/{team_id}/members"
        return await self._get_paginated(url)

    async def get_channel_messages(
        self,
        team_id: str,
        channel_id: str,
        since: str | None = None,
        top: int = 50,
    ) -> list[dict]:
        """Fetch messages from a Teams channel.

        Note: Channel messages endpoint has limited $filter support.
        We filter by date in Python to avoid 400 errors.
        """
        url = f"{GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/messages"
        params: dict = {"$top": str(top)}
        messages = await self._get_paginated(url, params=params)
        if since:
            messages = [m for m in messages if m.get("createdDateTime", "") >= since]
        return messages

    async def get_chats(self) -> list[dict]:
        """GET /me/chats — list all chats (1:1, group, meeting)."""
        url = f"{GRAPH_BASE_URL}/me/chats"
        params = {"$expand": "members", "$top": "50"}
        return await self._get_paginated(url, params=params)

    async def get_chat_messages(
        self,
        chat_id: str,
        since: str | None = None,
        top: int = 50,
    ) -> list[dict]:
        """Fetch messages from a specific chat.

        Note: The /me/chats/{id}/messages endpoint does NOT support $filter.
        We fetch all messages and filter by date in Python if `since` is provided.
        """
        url = f"{GRAPH_BASE_URL}/me/chats/{chat_id}/messages"
        params: dict = {"$top": str(top)}
        # Do NOT pass $filter — this endpoint doesn't support it
        messages = await self._get_paginated(url, params=params)
        if since:
            messages = [m for m in messages if m.get("createdDateTime", "") >= since]
        return messages

    async def close(self) -> None:
        """Shut down the underlying httpx client."""
        await self._http.aclose()
