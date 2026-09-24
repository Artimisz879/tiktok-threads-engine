"""Product intake service: URL in -> Product row out (state machine driven)."""
from __future__ import annotations

import hashlib
import logging

import httpx
from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import get_session
from app.models import Product, SystemEvent, TelegramJob
from app.product.providers import ProductResolver, ResolverError
from app.schemas import ResolvedProduct
from app.security.url_validation import URLValidationError, is_allowed_url
from app.state_machine import StateError, is_terminal, transition
from app.utils.timeutil import now_utc

log = logging.getLogger("product.service")


def product_key_for(url: str) -> str:
    """Stable dedupe key: hash of the normalised submitted URL."""
    norm = url.strip().rstrip("/").lower()
    return hashlib.sha256(norm.encode()).hexdigest()[:40]


class ProductService:
    def __init__(self, settings: Settings | None = None, session_factory=None):
        self.s = settings or get_settings()
        self.session_factory = session_factory or get_session
        self.resolver = ProductResolver(self.s)

    # ---------- submission ----------

    def submit(
        self, url: str, *, mode: str = "smart", chat_id: int | None = None
    ) -> tuple[Product, bool]:
        """Validate + insert (or find existing) product. Returns (product, is_new)."""
        url = url.strip()
        if not is_allowed_url(url):
            raise URLValidationError(
                "That URL is not a supported shop link. "
                "Supported: tiktok.com, vt.tiktok.com, shopee.com.my, lazada.com.my, temu.com."
            )
        key = product_key_for(url)
        with self.session_factory() as db:
            existing = db.scalar(select(Product).where(Product.product_key == key))
            if existing is not None:
                if is_terminal(existing.state) and existing.state != "COMPLETED":
                    # Allow retry of failed products by re-submitting.
                    transition(existing.state, "RESOLVING")
                    existing.state = "RESOLVING"
                    existing.state_reason = "re-submitted"
                    db.commit()
                    return existing, False
                return existing, False
            p = Product(
                product_key=key,
                original_url=url,
                state="RECEIVED",
                mode=mode,
            )
            db.add(p)
            if chat_id is not None:
                db.add(TelegramJob(chat_id=chat_id, product_id=None, mode=mode))
            db.commit()
            db.refresh(p)
            if chat_id is not None:
                job = db.scalar(
                    select(TelegramJob).where(TelegramJob.chat_id == chat_id).order_by(TelegramJob.id.desc())
                )
                if job is not None:
                    job.product_id = p.id
                    db.commit()
            return p, True

    # ---------- resolution ----------

    async def resolve_product(self, product_id: int) -> Product:
        with self.session_factory() as db:
            p = db.get(Product, product_id)
            if p is None:
                raise ValueError(f"product {product_id} not found")
            if p.state not in ("RECEIVED", "RESOLVING"):
                raise ValueError(f"product {product_id} not in resolvable state ({p.state})")
            if p.state != "RESOLVING":
                transition(p.state, "RESOLVING")
            p.state = "RESOLVING"
            db.commit()
            url = p.original_url

        try:
            async with httpx.AsyncClient() as client:
                data: ResolvedProduct = await self.resolver.resolve(url, client)
        except ResolverError as e:
            self._fail(product_id, f"URL could not be resolved: {e}")
            raise

        with self.session_factory() as db:
            p = db.get(Product, product_id)
            p.resolved_url = data.resolved_url or url
            p.product_id = data.product_id
            p.title = data.title or p.title
            p.price = data.price
            p.currency = data.currency
            p.description = data.description
            p.images = data.images
            p.seller = data.seller
            p.features = data.features
            p.confidence = data.confidence
            p.resolved_at = now_utc()
            transition(p.state, "ANALYZED")
            p.state = "ANALYZED"
            p.state_reason = f"resolved via {data.source} (confidence {data.confidence:.2f})"
            db.commit()
            db.refresh(p)
            return p

    # ---------- manual fallback ----------

    def supply_manual_info(self, product_id: int, title: str, price: float | None = None, category: str = "") -> Product:
        """User typed the product name because metadata was too thin."""
        with self.session_factory() as db:
            p = db.get(Product, product_id)
            if p is None:
                raise ValueError("product not found")
            p.title = title[:300]
            if price is not None:
                p.price = price
            p.category = category or p.category
            p.confidence = max(p.confidence, 0.6)
            p.state_reason = (p.state_reason + " | manual info supplied").strip(" |")
            db.commit()
            db.refresh(p)
            return p

    # ---------- helpers ----------

    def _fail(self, product_id: int, reason: str) -> None:
        with self.session_factory() as db:
            p = db.get(Product, product_id)
            if p is None or is_terminal(p.state):
                return
            try:
                transition(p.state, "FAILED")
            except StateError:
                return
            p.state = "FAILED"
            p.state_reason = reason[:500]
            db.add(SystemEvent(level="error", category="product", message=f"product {product_id} failed: {reason[:300]}"))
            db.commit()

    def get(self, product_id: int) -> Product | None:
        with self.session_factory() as db:
            p = db.get(Product, product_id)
            if p is not None:
                db.expunge(p)
            return p

    def list_by_state(self, *states: str) -> list[Product]:
        with self.session_factory() as db:
            rows = db.scalars(select(Product).where(Product.state.in_(states))).all()
            for r in rows:
                db.expunge(r)
            return rows

    def set_state(self, product_id: int, state: str, reason: str = "") -> None:
        with self.session_factory() as db:
            p = db.get(Product, product_id)
            if p is None:
                return
            if p.state != state:
                transition(p.state, state)
            p.state = state
            if reason:
                p.state_reason = reason[:500]
            db.commit()

    def log_event(self, level: str, category: str, message: str, detail: dict | None = None) -> None:
        try:
            with self.session_factory() as db:
                db.add(SystemEvent(level=level, category=category, message=message[:1000], detail=detail))
                db.commit()
        except Exception:
            log.exception("failed to log system event")
