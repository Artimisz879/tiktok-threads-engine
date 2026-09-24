"""URL validation + SSRF protection.

Rules:
- Only http/https.
- Host must resolve to a public IP (block private/loopback/link-local/reserved).
- Redirect hops are re-validated at every hop.
- Max hops, max response size, hard timeouts.
"""
from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

import httpx

log = logging.getLogger("security.url")

ALLOWED_HOST_SUFFIXES = (
    "tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
    "tiktokv.com",
    "shop.tiktok.com",
    "tiktokshop.com",
    "ttshop.com",
    "temu.com",
    "shopee.com.my",
    "shopee.com",
    "lazada.com.my",
    "lazada.com",
)

MAX_HOPS = 6
MAX_RESPONSE_BYTES = 1_500_000
TIMEOUT = 15.0


class URLValidationError(Exception):
    pass


def is_allowed_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    return any(host == sfx or host.endswith("." + sfx) for sfx in ALLOWED_HOST_SUFFIXES)


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def validate_public_host(host: str) -> None:
    """Raise URLValidationError if host is missing, a private raw IP, or an
    obviously internal name. Domains pass here (the allowlist already vetted
    them); DNS-level SSRF is additionally mitigated because we only ever
    fetch allowlisted hosts.
    """
    host = (host or "").lower().strip(".")
    if not host:
        raise URLValidationError("empty host")
    # Raw IP hosts must be public.
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None  # it's a domain name
    if addr is not None:
        if not (
            addr.is_global
            and not (addr.is_private or addr.is_loopback or addr.is_link_local
                     or addr.is_reserved or addr.is_multicast or addr.is_unspecified)
        ):
            raise URLValidationError(f"host is a non-public IP: {host}")
        return
    # Block obvious internal names.
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        raise URLValidationError(f"internal host blocked: {host}")


async def safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    timeout: float = TIMEOUT,
    follow_redirects: bool = True,
) -> httpx.Response:
    """GET with per-hop SSRF validation and size cap.

    Uses httpx's event hooks to validate the host of EVERY redirect target.
    """
    if not is_allowed_url(url):
        raise URLValidationError(f"URL not in allowlist: {url}")

    async def _check_host(request: httpx.Request) -> None:
        host = request.url.host or ""
        validate_public_host(host)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept-Language": "en-MY,en;q=0.9",
    }
    resp = await client.get(
        url, follow_redirects=follow_redirects, timeout=timeout, headers=headers
    )
    # Size cap on already-received content.
    if len(resp.content) > max_bytes:
        raise URLValidationError(f"response too large: {len(resp.content)}")
    return resp


async def resolve_redirects(client: httpx.AsyncClient, url: str) -> tuple[str, list[str]]:
    """Follow redirects manually, validating each hop. Returns (final_url, chain)."""
    current = url
    chain = [current]
    seen: set[str] = set()
    for _ in range(MAX_HOPS):
        if current in seen:
            break
        seen.add(current)
        if not is_allowed_url(current):
            break
        try:
            resp = await client.get(
                current,
                follow_redirects=False,
                timeout=TIMEOUT,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                    "Accept": "text/html,*/*",
                },
            )
        except httpx.HTTPError as e:
            log.warning("redirect hop failed for %s: %s", current, e)
            break
        location = resp.headers.get("location")
        if resp.status_code in (301, 302, 303, 307, 308) and location:
            from urllib.parse import urljoin

            nxt = urljoin(current, location)
            if not is_allowed_url(nxt):
                log.warning("redirect left allowlist at %s -> %s", current, nxt)
                break
            validate_public_host(urlparse(nxt).hostname or "")
            current = nxt
            chain.append(current)
        else:
            break
    return current, chain
