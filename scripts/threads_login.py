"""Threads OAuth login helper.

Usage:
    python -m scripts.threads_login

Flow:
1. Prints an authorization URL (open it in your browser).
2. Starts a tiny local HTTP server on the redirect URI port.
3. You log in with your Threads account and approve.
4. The script exchanges the short-lived code/token for a 60-day
   long-lived token and appends it to your .env file.

Requires THREADS_APP_ID and THREADS_APP_SECRET in .env first.
"""
from __future__ import annotations

import http.server
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

import httpx

from app.config import get_settings
from app.publishing.threads_client import ThreadsClient


def main() -> None:
    s = get_settings()
    if not s.threads_app_id or not s.threads_app_secret:
        print("ERROR: set THREADS_APP_ID and THREADS_APP_SECRET in .env first.")
        raise SystemExit(1)

    client = ThreadsClient(s)
    state = secrets.token_urlsafe(16)
    url = client.oauth_authorize_url(state)

    print("=" * 70)
    print("Open this URL in your browser and log in to Threads:")
    print()
    print(url)
    print()
    print("Waiting for the redirect... (Ctrl+C to cancel)")
    print("=" * 70)

    # The Threads OAuth redirect delivers `code` (authorization code).
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "error" in params:
                result["error"] = params["error"][0]
            elif "code" in params:
                result["code"] = params["code"][0]
                result["state"] = params.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Threads login received.</h2>"
                b"You can close this tab and go back to the terminal.</body></html>"
            )

        def log_message(self, *args):  # silence
            pass

    # Determine the port from the redirect URI.
    redirect = s.threads_redirect_uri
    port = int(urllib.parse.urlparse(redirect).port or 8321)
    host = urllib.parse.urlparse(redirect).hostname or "127.0.0.1"

    httpd = http.server.HTTPServer((host, port), Handler)
    t = threading.Thread(target=httpd.handle_request, daemon=True)
    t.start()

    timeout = 300
    start = time.time()
    while not result and time.time() - start < timeout:
        time.sleep(0.5)
    httpd.server_close()

    if "error" in result:
        print(f"OAuth error: {result['error']}")
        raise SystemExit(1)
    if "code" not in result:
        print("Timed out waiting for the redirect. Try again.")
        raise SystemExit(1)
    if result.get("state") != state:
        print("State mismatch — possible CSRF. Aborting.")
        raise SystemExit(1)

    print("Got authorization code. Exchanging for long-lived token...")

    # Threads/Meta: the redirect 'code' is exchanged at /oauth/access_token
    # (or the code IS a short-lived access token in some flows). We try the
    # standard token endpoint first, then fall back to treating it as a token.
    async def exchange() -> tuple[str, int]:
        async with httpx.AsyncClient(timeout=20.0) as http:
            # Standard OAuth token exchange
            resp = await http.post(
                f"https://graph.threads.com/{s.threads_api_version}/oauth/access_token",
                data={
                    "client_id": s.threads_app_id,
                    "client_secret": s.threads_app_secret,
                    "redirect_uri": s.threads_redirect_uri,
                    "code": result["code"],
                },
            )
            data = resp.json()
            if "access_token" in data:
                token = data["access_token"]
                expires = int(data.get("expires_in", 0))
                # Upgrade to long-lived if short-lived.
                if expires and expires < 60 * 86400:
                    ll_token, ll_exp = await client.exchange_long_lived(token)
                    return ll_token, ll_exp
                return token, expires
        # Fallback: treat code as a short-lived token directly.
        return await client.exchange_long_lived(result["code"])

    import asyncio

    token, expires_in = asyncio.run(exchange())
    if not token:
        print("Token exchange failed.")
        raise SystemExit(1)

    # Resolve the Threads user ID.
    s.threads_access_token = token
    s.threads_app_id = s.threads_app_id  # keep

    async def whoami() -> str:
        c = ThreadsClient(s)
        data = await c.my_threads_user()
        return data["id"]

    try:
        user_id = asyncio.run(whoami())
    except Exception as e:
        print(f"Got token but could not resolve user id: {e}")
        user_id = input("Threads user ID (from your profile URL, optional): ").strip()

    # Write to .env
    env_path = Path(".env")
    lines = env_path.read_text().splitlines() if env_path.exists() else []

    def set_var(name: str, value: str) -> None:
        for i, line in enumerate(lines):
            if line.startswith(name + "="):
                lines[i] = f"{name}={value}"
                return
        lines.append(f"{name}={value}")

    set_var("THREADS_ACCESS_TOKEN", token)
    if user_id:
        set_var("THREADS_USER_ID", user_id)
    env_path.write_text("\n".join(lines) + "\n")

    days = expires_in / 86400 if expires_in else "?"
    print()
    print("=" * 70)
    print("✅ Success! Wrote to .env:")
    print(f"   THREADS_ACCESS_TOKEN=*** ({days:.0f} days)" if isinstance(days, float) else f"   THREADS_ACCESS_TOKEN=***")
    if user_id:
        print(f"   THREADS_USER_ID={user_id}")
    print()
    print("Restart the app to connect. Token lasts ~60 days; re-run this script before it expires.")
    print("=" * 70)


if __name__ == "__main__":
    main()
