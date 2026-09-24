"""Prompt-injection protection helpers.

External content (product pages, news, search results) is ALWAYS data.
This module provides the wrapping + a deterministic guard that scans
generated content for dangerous claims before anything is published.
"""
from __future__ import annotations

import re

from app.utils.text import clean_text, wrap_untrusted

# Deterministic safety guard: patterns that must NEVER appear in a post.
# (The LLM critic is a second layer; this one is cheap and runs always.)
BANNED_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\b(ignore (all |any )?(previous|prior|above) instructions)", "embedded instruction attempt"),
    (r"(?i)\b(system prompt)\b", "prompt reference"),
    (r"(?i)\b100% (cure|guarantee|refund)s?\b", "guarantee claim"),
    (r"(?i)\b(cures?|treats?|prevents?) (cancer|diabetes|dengue|flu|corona|virus)\b", "medical claim"),
    (r"(?i)\bmiracle\b", "miracle claim"),
    (r"(?i)\b(rm ?\d+ (cashback|bonus) (guaranteed|free))\b", "financial claim"),
    (r"(?i)\b(only \d+ (left|remaining))\b", "fake scarcity"),
    (r"(?i)\b(as seen on (tv|news))\b", "fake endorsement"),
    (r"(?i)\b(doctors? (recommend|use))\b", "fake authority"),
]

_SENSITIVE_TOPIC_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\b(meninggal|died|death|kematian|mayat|corpse)\b", "death"),
    (r"(?i)\b(earthquake|banjir besar|flood (disaster|victims)|tsunami)\b", "disaster"),
    (r"(?i)\b(accident (victim|death)|maut|terbakar hidup-hidup)\b", "accident/violence"),
    (r"(?i)\b(terror(is|ist)|bom (meletup|explosion))\b", "terrorism"),
    (r"(?i)\b(race riot|rasuah|corruption scandal)\b", "sensitive politics"),
    (r"(?i)\b(child (abuse|victim)|pedophilia)\b", "vulnerable individuals"),
    (r"(?i)\b(protest (violence|death)|demo (bentrok|kekerasan))\b", "violent protest"),
]


def sanitize_for_prompt(raw: str, label: str = "untrusted_data") -> str:
    """Clean + wrap external content for safe inclusion in prompts."""
    return wrap_untrusted(clean_text(raw, max_chars=4000), label)


def scan_content(text: str) -> list[str]:
    """Return list of violations found in generated content (empty = clean)."""
    violations = []
    for pattern, label in BANNED_PATTERNS:
        if re.search(pattern, text):
            violations.append(f"banned: {label}")
    return violations


def scan_trend_sensitivity(text: str) -> list[str]:
    """Return sensitivity hits for a trend topic (empty = safe to market)."""
    hits = []
    for pattern, label in _SENSITIVE_TOPIC_PATTERNS:
        if re.search(pattern, text):
            hits.append(label)
    return hits
