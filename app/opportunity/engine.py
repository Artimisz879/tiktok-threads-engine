"""Opportunity engine: score product x trend combinations.

OpportunityScore (0-100) =
    100 * velocity * relevance * audience_fit * freshness
    - sensitivity_penalty - spam_risk_penalty

- velocity:      trend velocity (0..1) from the radar
- relevance:     LLM relevance score (0..1)
- audience_fit:  deterministic 1.0 (audience matching is folded into relevance
                 by the LLM; we do not fake a separate number)
- freshness:     1.0 if trend seen < 24h, decays after
- penalties:     sensitive trend = hard reject; low-confidence trend = penalty

A trend must clear OPPORTUNITY_THRESHOLD to be used. Otherwise the product
falls back to evergreen content (never a forced trend).
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.config import Settings, get_settings
from app.utils.timeutil import hours_since, now_utc

log = logging.getLogger("opportunity")


def score_opportunity(
    *,
    velocity: float,
    relevance: float,
    trend_age_hours: float,
    confidence: float,
    sensitivity: str = "neutral",
) -> tuple[float, dict]:
    """Returns (score 0-100, breakdown). Sensitive trends return (0, breakdown)."""
    if sensitivity.startswith("sensitive"):
        return 0.0, {"reject": "sensitive trend", "sensitivity": sensitivity}

    freshness = 1.0 if trend_age_hours <= 24 else max(0.0, 1.0 - (trend_age_hours - 24) / 24.0)
    base = velocity * relevance * freshness
    # Confidence penalty: low-confidence trends score lower.
    confidence_factor = 0.5 + 0.5 * confidence
    score = 100.0 * base * confidence_factor

    breakdown = {
        "velocity": round(velocity, 3),
        "relevance": round(relevance, 3),
        "freshness": round(freshness, 3),
        "confidence": round(confidence, 3),
        "confidence_factor": round(confidence_factor, 3),
        "score": round(score, 1),
    }
    return round(score, 1), breakdown


def best_opportunity(
    matches: list[dict], threshold: int, now: datetime | None = None
) -> dict | None:
    """Pick the best trend match that clears the threshold, else None (=> evergreen).

    matches: list of dicts from trend_matcher with keys:
      trend, relevance_score, reason, angle_hint, topic, velocity, sensitivity, ...
    """
    now = now or now_utc()
    best = None
    best_score = 0.0
    for m in matches:
        trend = m["trend"]
        age_h = hours_since(trend.last_seen)
        score, breakdown = score_opportunity(
            velocity=m.get("velocity", trend.velocity),
            relevance=m.get("relevance_score", 0.0),
            trend_age_hours=age_h,
            confidence=trend.confidence,
            sensitivity=trend.sensitivity,
        )
        breakdown["topic"] = trend.topic
        breakdown["reason"] = m.get("reason", "")
        m["score"] = score
        m["breakdown"] = breakdown
        if score >= threshold and score > best_score:
            best = m
            best_score = score
    return best


def evergreen_opportunity() -> dict:
    """The fallback: no trend, just write good evergreen content."""
    return {
        "trend": None,
        "topic": None,
        "kind": "evergreen",
        "score": 0.0,
        "breakdown": {"reason": "no trend cleared threshold; using evergreen content"},
        "relevance_score": 0.0,
        "reason": "evergreen fallback",
        "angle_hint": "",
        "velocity": 0.0,
        "sensitivity": "neutral",
    }
