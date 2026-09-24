"""Content strategist + writer: LLM call 3. Generates 5 diverse candidates."""
from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.intelligence.prompts import candidate_generation_system, candidate_generation_user
from app.llm.client import LLMClient, get_llm
from app.models import Product
from app.schemas import CandidateBatch, ProductIntelligence

log = logging.getLogger("intelligence.strategist")


def product_brief(product: Product, intelligence: ProductIntelligence | None = None) -> str:
    price = f"RM{product.price:.2f}" if product.price else "price unknown"
    base = f"{product.title or 'product'} ({price})"
    if intelligence:
        base += f" for {', '.join(intelligence.target_audiences[:3])}"
    return base


async def generate_candidates(
    product: Product,
    intelligence: ProductIntelligence,
    trend: dict | None,
    strategy_notes: str = "",
    llm: LLMClient | None = None,
    settings: Settings | None = None,
    posts_today: int = 0,
) -> list[dict]:
    """Return list of candidate dicts: {angle, hook_type, content, why_it_works}."""
    llm = llm or get_llm()
    s = settings or get_settings()
    out: CandidateBatch = llm.generate_structured(
        CandidateBatch,
        candidate_generation_system(),
        candidate_generation_user(
            product_brief(product, intelligence),
            intelligence.model_dump(),
            trend,
            strategy_notes,
            s.content_language,
            s.affiliate_disclosure_text if s.affiliate_disclosure_enabled else "",
            posts_today,
        ),
        purpose="candidate_generation",
        temperature=s.llm_temperature,
    )
    return [c.model_dump() for c in out.candidates]
