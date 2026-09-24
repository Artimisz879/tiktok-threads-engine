"""Deterministic offline LLM provider for tests and credential-free demos.

It produces plausible, schema-valid outputs based on simple keyword logic.
This is NOT a quality writer — it exists so the whole pipeline can be
exercised end-to-end without any API key.
"""
from __future__ import annotations

import json
import re

_HOT_KEYWORDS = {"panas", "heat", "hot", "cuaca", "weather", "summer", "aircond", "ac "}
_FAN_KEYWORDS = {"fan", "kipas", "cool", "turbo"}


def _has_any(text: str, keywords: set[str]) -> bool:
    t = text.lower()
    return any(k in t for k in keywords)


def mock_chat(system: str, user: str) -> str:
    """Route on markers in the system prompt to produce the right schema."""
    if "ProductIntelligence" in system or "product_summary" in system:
        return _mock_intelligence(user)
    if "TrendRelevanceBatch" in system or "relevance_score" in system:
        return _mock_relevance(user)
    if "CandidateBatch" in system or "hook_type" in system:
        return _mock_candidates(user)
    if "CriticReport" in system or "winner_index" in system:
        return _mock_critic(user)
    return json.dumps({"text": "mock response"})


def _mock_intelligence(user: str) -> str:
    text = user.lower()
    is_fan = _has_any(text, _FAN_KEYWORDS)
    hot = _has_any(text, _HOT_KEYWORDS)
    if is_fan:
        data = {
            "product_summary": "A portable USB rechargeable fan, small and cheap, aimed at beating Malaysian heat on the go.",
            "target_audiences": ["university students", "commuters", "office workers"],
            "problems_solved": ["staying cool in hot rooms", "beating heat while commuting"],
            "pain_points": ["Malaysia is always hot", "public transport is stuffy", "classrooms without AC"],
            "emotional_triggers": ["relief", "comfort", "affordable treat"],
            "purchase_motivations": ["cheap", "portable", "immediate comfort"],
            "content_opportunities": ["hot weather discussions", "commuting struggles", "dorm life"],
            "risky_claims_to_avoid": ["medical claims", "battery life exaggeration"],
            "recommended_tone": ["casual", "observational", "light humour"],
            "category": "personal cooling",
            "price_range": "under_rm50",
        }
    else:
        data = {
            "product_summary": "An affordable everyday product with a clear practical benefit for Malaysian buyers.",
            "target_audiences": ["young Malaysians", "students"],
            "problems_solved": ["a small everyday inconvenience"],
            "pain_points": ["daily minor frustrations"],
            "emotional_triggers": ["convenience", "value"],
            "purchase_motivations": ["cheap", "easy"],
            "content_opportunities": ["relatable everyday observations"],
            "risky_claims_to_avoid": ["unverified claims"],
            "recommended_tone": ["casual", "honest"],
            "category": "general",
            "price_range": "under_rm50",
        }
    return json.dumps(data, ensure_ascii=False)


def _mock_relevance(user: str) -> str:
    # Extract trend ids mentioned in the user prompt.
    ids = [int(x) for x in re.findall(r"trend_id[:\s]+(\d+)", user, re.IGNORECASE)]
    if not ids:
        ids = [1]
    results = []
    for i, tid in enumerate(ids):
        hot = _has_any(user, _HOT_KEYWORDS)
        fan = _has_any(user, _FAN_KEYWORDS)
        relevant = hot and fan
        results.append(
            {
                "trend_id": tid,
                "relevant": relevant,
                "relevance_score": 0.9 if relevant else 0.2,
                "reason": "heat discussion naturally fits a cooling product" if relevant else "no natural connection",
                "angle_hint": "tie the heat complaint to the fan as the fix" if relevant else "",
            }
        )
    return json.dumps({"results": results}, ensure_ascii=False)


def _mock_candidates(user: str) -> str:
    candidates = [
        {
            "angle": "relatable humour",
            "hook_type": "humour",
            "content": "malaysia at 2pm: officially a soup. i stopped fighting it and just got a little fan that fits in my bag. my neck has never been happier. (affiliate link)",
            "why_it_works": "dry observation, no hard sell",
        },
        {
            "angle": "mini story",
            "hook_type": "story",
            "content": "walked 10 minutes from the stop to campus yesterday and honestly forgot water existed. now i keep a tiny fan in my bag like it is a phone charger. essential survival gear. (affiliate link)",
            "why_it_works": "story leads to product",
        },
        {
            "angle": "problem solution",
            "hook_type": "problem_solution",
            "content": "if your commute involves standing in a hot bus, do yourself a favour and get one of these little fans. rm19 and it genuinely makes the ride bearable. (affiliate link)",
            "why_it_works": "practical, price-led",
        },
        {
            "angle": "curiosity question",
            "hook_type": "question",
            "content": "what is the one cheap thing that actually made your daily life better this year? for me it is a portable fan. small thing, huge difference. (affiliate link)",
            "why_it_works": "invites replies",
        },
        {
            "angle": "observation",
            "hook_type": "observation",
            "content": "people talk about saving money but will happily spend on iced coffee daily. meanwhile a rm19 fan lasts months. priorities, honestly. (affiliate link)",
            "why_it_works": "contrarian observation",
        },
    ]
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def _mock_critic(user: str) -> str:
    # Find how many candidates were submitted.
    n = len(re.findall(r'"content":\s*"', user))
    n = max(1, min(n, 8))
    verdicts = []
    for i in range(n):
        verdicts.append(
            {
                "candidate_index": i,
                "human_sounding": 0.8,
                "humour": 0.6,
                "curiosity": 0.5,
                "relatability": 0.8,
                "product_relevance": 0.7,
                "audience_relevance": 0.7,
                "originality": 0.6,
                "usefulness": 0.6,
                "purchase_intent": 0.5,
                "trend_relevance": 0.4,
                "ai_sounding_penalty": 0.1,
                "overly_salesy_penalty": 0.1,
                "misinformation_risk": 0.0,
                "reputational_risk": 0.0,
                "policy_risk": 0.0,
                "safe": True,
                "rejection_reason": "",
                "improved_content": None,
                "overall": 0.72,
            }
        )
    return json.dumps(
        {
            "verdicts": verdicts,
            "winner_index": 0,
            "publish": True,
            "notes": "mock critic: first candidate wins",
        },
        ensure_ascii=False,
    )
