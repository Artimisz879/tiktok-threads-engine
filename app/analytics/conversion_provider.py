"""Conversion provider abstraction.

V1 ships NoConversionProvider (honest: data unavailable). When TikTok Shop
API access is granted, implement TikTokAffiliateAPIProvider with the same
interface and flip the factory. Nothing else in the system changes.
"""
from __future__ import annotations

import abc
import logging
from datetime import datetime

from app.config import Settings, get_settings
from app.database import get_session
from app.models import AffiliateConversion
from app.utils.timeutil import now_utc

log = logging.getLogger("analytics.conversion")


class ConversionProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def fetch(self, post_id: int | None, product_id: int | None) -> dict | None:
        """Return {orders, estimated_commission, currency, raw} or None."""


class NoConversionProvider(ConversionProvider):
    """No TikTok API access: report 'unavailable' without breaking anything."""

    name = "none"

    async def fetch(self, post_id: int | None, product_id: int | None) -> dict | None:
        return None


class TikTokAffiliateAPIProvider(ConversionProvider):
    """Stub for when TikTok Shop API access exists.

    Intended endpoints (TikTok Shop Open Platform, affiliate role):
      - product order list / sales summary
      - commission summary
    Wire the real calls here; keep the return shape identical.
    """

    name = "tiktok_api"

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    async def fetch(self, post_id: int | None, product_id: int | None) -> dict | None:
        log.debug("TikTokAffiliateAPIProvider not wired yet; returning None")
        return None


class ConversionService:
    def __init__(self, settings: Settings | None = None, session_factory=None):
        self.s = settings or get_settings()
        self.session_factory = session_factory or get_session
        self.provider: ConversionProvider = (
            TikTokAffiliateAPIProvider(self.s) if self.s.tiktok_api_enabled else NoConversionProvider()
        )

    async def collect(self, post_id: int | None, product_id: int | None) -> AffiliateConversion | None:
        result = await self.provider.fetch(post_id, product_id)
        if result is None:
            return None
        with self.session_factory() as db:
            row = AffiliateConversion(
                post_id=post_id,
                product_id=product_id,
                source=self.provider.name,
                orders=result.get("orders"),
                estimated_commission=result.get("estimated_commission"),
                currency=result.get("currency", "MYR"),
                raw=result.get("raw"),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def status_summary(self) -> str:
        if self.s.tiktok_api_enabled:
            return "TikTok API connected"
        return "Unavailable until TikTok API connected"
