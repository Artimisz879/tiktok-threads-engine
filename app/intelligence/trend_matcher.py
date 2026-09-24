"""Trend relevance matcher: LLM call 2 (batched).

One call evaluates the top-N active trends against one product.
Returns a list of (trend, relevance_score, angle_hint) sorted by score.
"""
from __future__ import annotations

import logging

from app.intelligence.prompts import trend_relevance_system, trend_relevance_user
from app.intelligence.strategist import product_brief
from app.llm.client import LLMClient, get_llm
from app.models import Product, Trend
from app.schemas import ProductIntelligence, TrendRelevanceBatch

log = logging.getLogger("intelligence.matcher")


async def match_trends(
    product: Product,
    intelligence: ProductIntelligence,
    trends: list[Trend],
    llm: LLMClient | None = None,
    top_n: int = 8,
) -> list[dict]:
    """Evaluate trends against the product. Returns list of dicts:
    {trend, relevance_score, reason, angle_hint} sorted desc by relevance."""
    if not trends:
        return []
    llm = llm or get_llm()
    trend_dicts = [
        {
            "id": t.id,
            "topic": t.topic,
            "keywords": t.keywords or [],
            "sentiment": t.sentiment,
            "sensitivity": t.sensitivity,
            "velocity": t.velocity,
        }
        for t in trends[:top_n]
    ]
    batch: TrendRelevanceBatch = llm.generate_structured(
        TrendRelevanceBatch,
        trend_relevance_system(),
        trend_relevance_user(product_brief(product, intelligence), trend_dicts),
        purpose="trend_relevance",
        temperature=0.1,
    )
    by_id = {t.id: t for t in trends}
    results = []
    for r in batch.results:
        t = by_id.get(r.trend_id)
        if t is None:
            continue
        results.append(
            {
                "trend": t,
                "relevance_score": r.relevance_score,
                "reason": r.reason,
                "angle_hint": r.angle_hint,
                "topic": t.topic,
                "keywords": t.keywords,
                "velocity": t.velocity,
                "sentiment": t.sentiment,
                "sensitivity": t.sensitivity,
            }
        )
    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    return results
