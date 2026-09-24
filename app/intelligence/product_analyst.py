"""Product analyst: LLM call 1. Structured intelligence from product data."""
from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.intelligence.prompts import product_intelligence_system, product_intelligence_user
from app.llm.client import LLMClient, get_llm
from app.models import Product
from app.schemas import ProductIntelligence

log = logging.getLogger("intelligence.analyst")

# Cache: product_id -> intelligence (persisted on the product row itself,
# so re-analysis never happens for the same product).


async def analyze_product(
    product: Product, llm: LLMClient | None = None, settings: Settings | None = None
) -> ProductIntelligence:
    """Run (or reuse) product intelligence. Idempotent: skips if already analyzed."""
    if product.analysis:
        return ProductIntelligence.model_validate(product.analysis)

    llm = llm or get_llm()
    data = {
        "title": product.title,
        "price": product.price,
        "currency": product.currency,
        "description": (product.description or "")[:1500],
        "category": product.category,
        "seller": product.seller,
        "features": product.features or [],
        "confidence": product.confidence,
    }
    out: ProductIntelligence = llm.generate_structured(
        ProductIntelligence,
        product_intelligence_system(),
        product_intelligence_user(data),
        purpose="product_intelligence",
        temperature=0.4,
    )
    return out
