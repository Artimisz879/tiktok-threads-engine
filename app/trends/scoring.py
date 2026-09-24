"""Trend velocity scoring.

Deterministic heuristics only (no LLM). Scores are decision aids, not
precise measurements. Each component is clamped 0..1:

  velocity = 0.25*recency + 0.25*growth + 0.20*engagement
           + 0.15*cross_source + 0.15*malaysia_relevance

- recency:        how fresh the latest observation is (half-life 6h)
- growth:         volume now vs volume at last snapshot (capped)
- engagement:     log-scaled discussion volume (Threads likes/posts if present)
- cross_source:   presence in multiple independent sources (news + threads + web)
- malaysia_rel:   keyword overlap with a small Malaysian-context lexicon
"""
from __future__ import annotations

import math
from datetime import datetime

from app.utils.timeutil import hours_since, now_utc

MALAY_KEYWORDS = {
    "malaysia", "m'sia", "msia", "malaysian", "bangla", "kuala lumpur", "kl",
    "selangor", "penang", "johor", "sabah", "sarawak", "kuching", "ipoh",
    "shah alam", "putrajaya", "mrt", "lrt", "rapid", "petronas", "rm",
    "makan", "nasi", "kopi", "mamak", "kopitiam", "balik kampung", "raya",
    "puasa", "hari raya", "cukai", "tunai", "gaji", "skim", "kementerian",
    "denda", "lambak", "traffic", "panas", "banjir", "cuaca", "monsun",
    "student", "uni", "kampus", "kolej", "tuis", "uitm", "um", "ukm",
}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def recency_score(last_seen: datetime | None, now: datetime | None = None) -> float:
    now = now or now_utc()
    if last_seen is None:
        return 0.0
    age_h = hours_since(last_seen)
    if age_h < 0:
        age_h = 0.0
    # Half-life 6h: 6h old = 0.5, 24h old = 0.0625
    return _clamp01(0.5 ** (age_h / 6.0))


def growth_score(prev_volume: int | None, curr_volume: int) -> float:
    if prev_volume is None or prev_volume <= 0:
        # First observation: neutral-low (we don't know if it's growing)
        return 0.3 if curr_volume > 0 else 0.0
    ratio = curr_volume / prev_volume
    if ratio >= 2.0:
        return 1.0
    if ratio <= 0.5:
        return 0.0
    return _clamp01((ratio - 0.5) / 1.5)


def engagement_score(volume: int) -> float:
    if volume <= 0:
        return 0.0
    return _clamp01(math.log10(1 + volume) / 3.0)  # 1000+ -> 1.0


def cross_source_score(source_count: int) -> float:
    return _clamp01(source_count / 2.0)  # 2+ independent sources = full


def malaysia_relevance_score(keywords: list[str], topic: str = "") -> float:
    text = (" ".join(keywords + ([topic] if topic else [])) + " ").lower()
    import re as _re

    hits = 0
    for entry in MALAY_KEYWORDS:
        if " " in entry:
            if entry in text:
                hits += 1
        else:
            if _re.search(r"\b" + _re.escape(entry) + r"\b", text):
                hits += 1
    return _clamp01(hits / 3.0)  # 3+ Malay-context words = full


def compute_velocity(
    *,
    last_seen: datetime | None,
    prev_volume: int | None,
    curr_volume: int,
    source_count: int,
    keywords: list[str],
    topic: str = "",
    now: datetime | None = None,
) -> float:
    r = recency_score(last_seen, now)
    g = growth_score(prev_volume, curr_volume)
    e = engagement_score(curr_volume)
    c = cross_source_score(source_count)
    m = malaysia_relevance_score(keywords, topic)
    score = 0.25 * r + 0.25 * g + 0.20 * e + 0.15 * c + 0.15 * m
    return round(_clamp01(score), 3)
