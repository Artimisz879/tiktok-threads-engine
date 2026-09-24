"""Product resolver providers.

ProductResolver (facade)
  -> TikTokAPIResolver        (official API, disabled by default)
  -> PublicMetadataResolver   (redirect + og: tags + JSON-LD + oEmbed)
  -> ManualFallbackResolver   (user supplies the product name via Telegram)

Each provider returns a ResolvedProduct with an honest confidence score.
The facade merges the best available data. Missing data stays missing —
never fabricated.
"""
from __future__ import annotations

import abc
import json
import logging
import re
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.schemas import ResolvedProduct
from app.security.url_validation import (
    URLValidationError,
    is_allowed_url,
    resolve_redirects,
    safe_get,
)
from app.utils.text import clean_text

log = logging.getLogger("product")

PRICE_RE = re.compile(
    r"(?:rm|myr|₨)\s?(\d{1,6}(?:\.\d{1,2})?)"
    r"|(\d{1,6}(?:\.\d{1,2})?)\s?(?:rm|myr)",
    re.IGNORECASE,
)


class ResolverError(Exception):
    pass


class BaseResolver(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def resolve(self, url: str, client: httpx.AsyncClient) -> ResolvedProduct | None:
        """Return partial/complete product data, or None if this provider can't help."""


class TikTokAPIResolver(BaseResolver):
    """Official TikTok Shop Open Platform resolver.

    Disabled unless TIKTOK_API_ENABLED=true AND a token is configured.
    Kept as a clean adapter: when you get API access, fill in the real
    endpoints and the rest of the system is unchanged.
    """

    name = "tiktok_api"

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self.s.tiktok_api_enabled and bool(self.s.tiktok_api_access_token)

    async def resolve(self, url: str, client: httpx.AsyncClient) -> ResolvedProduct | None:
        if not self.enabled:
            return None
        # NOTE: real endpoint wiring goes here once API access is granted.
        log.info("TikTok API resolver enabled but not wired yet; skipping")
        return None


class PublicMetadataResolver(BaseResolver):
    """Resolve redirects, fetch the final page, extract og: tags + JSON-LD."""

    name = "public_metadata"

    async def resolve(self, url: str, client: httpx.AsyncClient) -> ResolvedProduct | None:
        try:
            final_url, chain = await resolve_redirects(client, url)
        except URLValidationError as e:
            raise ResolverError(str(e)) from e

        html = ""
        try:
            resp = await safe_get(client, final_url)
            html = resp.text
        except (URLValidationError, httpx.HTTPError) as e:
            log.warning("metadata fetch failed for %s: %s", final_url, e)

        meta = self._extract_meta(html)
        meta["oembed"] = await self._oembed(client, final_url)

        title = meta.get("title", "")
        desc = meta.get("description", "")
        images = meta.get("images", [])
        price = self._extract_price(title + " " + desc + " " + meta.get("raw_text", ""))

        # Confidence: what did we actually get?
        score = 0.0
        if title:
            score += 0.45
        if desc:
            score += 0.25
        if images:
            score += 0.15
        if price is not None:
            score += 0.15
        if final_url != url:
            score += 0.0  # redirect resolved, neutral

        return ResolvedProduct(
            original_url=url,
            resolved_url=final_url,
            title=title[:300],
            price=price,
            currency="MYR" if price is not None else "MYR",
            description=desc[:1500],
            images=images[:8],
            confidence=round(min(score, 1.0), 2),
            source=self.name,
        )

    async def _oembed(self, client: httpx.AsyncClient, url: str) -> dict:
        """TikTok oEmbed for video pages (works for some share links)."""
        try:
            if "tiktok.com" not in url:
                return {}
            resp = await client.get(
                "https://www.tiktok.com/oembed",
                params={"url": url},
                timeout=10.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError:
            pass
        return {}

    def _extract_meta(self, html: str) -> dict[str, Any]:
        out: dict[str, Any] = {"title": "", "description": "", "images": [], "raw_text": ""}
        if not html:
            return out

        def og(prop: str) -> str:
            m = re.search(
                r'<meta[^>]+(?:property|name)=["\']og:' + re.escape(prop) + r'["\'][^>]+content=["\']([^"\']*)["\']',
                html,
                re.IGNORECASE,
            )
            if not m:
                m = re.search(
                    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']og:' + re.escape(prop) + r'["\']',
                    html,
                    re.IGNORECASE,
                )
            return m.group(1).strip() if m else ""

        out["title"] = clean_text(og("title") or self._title_tag(html), max_chars=300)
        out["description"] = clean_text(
            og("description") or self._meta_tag(html, "description"), max_chars=1500
        )
        for img in re.findall(
            r'<meta[^>]+(?:property|name)=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )[:8]:
            out["images"].append(img)
        if not out["images"]:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']', html, re.IGNORECASE)
            if m:
                out["images"] = [m.group(1)]

        # JSON-LD product block
        for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE):
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") in ("Product", "ProductGroup"):
                    out["title"] = out["title"] or clean_text(str(item.get("name", "")), 300)
                    out["description"] = out["description"] or clean_text(str(item.get("description", "")), 1500)
                    offers = item.get("offers") or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    if isinstance(offers, dict):
                        price = offers.get("price")
                        if price is not None:
                            try:
                                out["price_hint"] = float(price)
                            except (TypeError, ValueError):
                                pass
                    for img in item.get("image", []) if isinstance(item.get("image"), list) else [item.get("image")]:
                        if isinstance(img, str):
                            out["images"].append(img)
                    break
        out["raw_text"] = clean_text(html, max_chars=6000)
        if "price_hint" in out:
            out.setdefault("price", out.pop("price_hint"))
        return out

    @staticmethod
    def _title_tag(html: str) -> str:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _meta_tag(html: str, name: str) -> str:
        m = re.search(
            r'<meta[^>]+name=["\']' + re.escape(name) + r'["\'][^>]+content=["\']([^"\']*)["\']',
            html,
            re.IGNORECASE,
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']' + re.escape(name) + r'["\']',
                html,
                re.IGNORECASE,
            )
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_price(text: str) -> float | None:
        m = PRICE_RE.search(text)
        if not m:
            return None
        val = m.group(1) or m.group(2)
        try:
            price = float(val)
        except ValueError:
            return None
        if 0 < price < 100_000:
            return round(price, 2)
        return None


class ManualFallbackResolver(BaseResolver):
    """User provides the product name manually via Telegram. No fetching."""

    name = "manual"

    async def resolve(self, url: str, client: httpx.AsyncClient) -> ResolvedProduct | None:
        return None  # handled by ProductService when user replies with a name


class ProductResolver:
    """Facade: try providers in order, merge best data."""

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.providers: list[BaseResolver] = [
            TikTokAPIResolver(self.s),
            PublicMetadataResolver(),
        ]

    async def resolve(self, url: str, client: httpx.AsyncClient) -> ResolvedProduct:
        best: ResolvedProduct | None = None
        errors: list[str] = []
        for provider in self.providers:
            try:
                result = await provider.resolve(url, client)
            except ResolverError as e:
                errors.append(f"{provider.name}: {e}")
                continue
            except Exception as e:  # provider bugs must not kill intake
                log.exception("provider %s crashed", provider.name)
                errors.append(f"{provider.name}: {e}")
                continue
            if result is None:
                continue
            if best is None:
                best = result
            else:
                best = self._merge(best, result)
        if best is None:
            # Still return a skeleton so the user can supply a name manually.
            best = ResolvedProduct(original_url=url, confidence=0.0, source="none")
        if errors:
            log.warning("resolver errors: %s", "; ".join(errors))
        return best

    @staticmethod
    def _merge(a: ResolvedProduct, b: ResolvedProduct) -> ResolvedProduct:
        def pick(x, y):
            return x if x not in (None, "", [], 0.0) else y

        return ResolvedProduct(
            original_url=a.original_url,
            resolved_url=pick(a.resolved_url, b.resolved_url),
            product_id=pick(a.product_id, b.product_id),
            title=pick(a.title, b.title),
            price=pick(a.price, b.price),
            currency=pick(a.currency, b.currency),
            description=pick(a.description, b.description),
            images=list(dict.fromkeys(a.images + b.images))[:8],
            category=pick(a.category, b.category),
            seller=pick(a.seller, b.seller),
            features=list(dict.fromkeys(a.features + b.features)),
            confidence=max(a.confidence, b.confidence),
            source=f"{a.source}+{b.source}" if a.source != b.source else a.source,
        )
