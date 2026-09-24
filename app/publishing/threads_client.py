"""Official Threads API client.

Endpoints (verified against live API, Graph API v21.0):
  GET  /me                      -> identity check
  POST /me/threads              -> publish text post (message, link?)
  GET  /{id}/insights           -> per-post metrics
  GET  /threads_search          -> keyword search (ordering=top|recent)
  POST graph.threads.com /access_token -> short->long-lived exchange

Rate limits: ~250 calls/hour per user for publishing; search is separate.
We handle 429 with Retry-After and log everything.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings, get_settings

log = logging.getLogger("threads.client")

GRAPH = "https://graph.threads.net"
AUTH = "https://graph.threads.com"


class ThreadsAPIError(Exception):
    def __init__(self, message: str, code: int | None = None, http_status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after

    @property
    def is_rate_limit(self) -> bool:
        return self.http_status == 429 or self.code in (4, 17, 32, 613)

    @property
    def is_auth_error(self) -> bool:
        return self.code in (190, 299, 10) or self.http_status in (401, 403)


class ThreadsClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.base = f"{GRAPH}/{self.s.threads_api_version}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.s.threads_access_token}"}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(self.base + path, params=params, headers=self._headers())
        return self._parse(resp)

    async def _post(self, path: str, data: dict) -> dict:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(self.base + path, data=data, headers=self._headers())
        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except Exception:
            raise ThreadsAPIError(f"non-JSON response ({resp.status_code}): {resp.text[:200]}", http_status=resp.status_code)
        if resp.status_code >= 400 or "error" in data:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            msg = err.get("message", str(data))[:300]
            code = err.get("code")
            retry_after = None
            if resp.status_code == 429:
                ra = resp.headers.get("retry-after")
                try:
                    retry_after = float(ra)
                except (TypeError, ValueError):
                    retry_after = 60.0
            raise ThreadsAPIError(msg, code=code, http_status=resp.status_code, retry_after=retry_after)
        return data

    # ---------- public API ----------

    async def me(self) -> dict:
        data = await self._get("/me")
        return data

    async def my_threads_user(self) -> dict:
        """Resolve the Threads user ID for the token (threads_user_id field)."""
        data = await self._get("/me")
        uid = data.get("threads_user_id") or data.get("id")
        if not uid:
            raise ThreadsAPIError("could not determine threads_user_id from /me")
        return {"id": str(uid), "raw": data}

    async def publish_text(self, text: str, link: str | None = None) -> dict:
        """Publish a text post. Returns {id, permalink}."""
        data: dict[str, Any] = {"message": text}
        if link:
            data["link"] = link
        result = await self._post("/me/threads", data)
        post_id = result.get("id")
        if not post_id:
            raise ThreadsAPIError(f"publish returned no id: {str(result)[:200]}")
        permalink = f"https://www.threads.net/@{self.s.threads_user_id}/post/{post_id}"
        return {"id": str(post_id), "permalink": permalink}

    async def post_insights(self, post_id: str, metrics: list[str] | None = None) -> dict:
        """Fetch per-post insights.

        Verified metric names (v21.0): likes_count, replies_count, reposts_count,
        shares_count, external_action_count, views (views may be unavailable for
        some accounts; we request a broad set and keep whatever comes back).
        """
        metrics = metrics or [
            "likes_count",
            "replies_count",
            "reposts_count",
            "shares_count",
            "external_action_count",
            "views",
        ]
        return await self._get(f"/{post_id}/insights", {"metrics": ",".join(metrics)})

    async def search_threads(self, keywords: str, ordering: str = "top", limit: int = 25) -> dict:
        """threads_search: keywords (comma-sep, max 5), ordering top|recent, limit 1-100."""
        return await self._get(
            "/threads_search",
            {"keywords": keywords, "ordering": ordering, "limit": min(max(limit, 1), 100)},
        )

    # ---------- token management ----------

    async def exchange_long_lived(self, short_token: str) -> tuple[str, int]:
        """Exchange a short-lived token for a long-lived one (~60 days).
        Returns (token, expires_in_seconds)."""
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{AUTH}/{self.s.threads_api_version}/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "fb_exchange_token": short_token,
                    "app_id": self.s.threads_app_id,
                    "app_secret": self.s.threads_app_secret,
                },
            )
        data = resp.json()
        if "access_token" not in data:
            raise ThreadsAPIError(f"token exchange failed: {str(data)[:200]}")
        return data["access_token"], int(data.get("expires_in", 0))

    def oauth_authorize_url(self, state: str) -> str:
        from urllib.parse import quote

        return (
            f"https://www.threads.net/oauth/authorize"
            f"?client_id={self.s.threads_app_id}"
            f"&redirect_uri={quote(self.s.threads_redirect_uri, safe='')}"
            f"&scope=threads_basic,threads_content_publish,threads_engagement,threads_manage_insights"
            f"&state={quote(state)}"
        )
