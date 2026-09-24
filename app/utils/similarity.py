"""Cheap deterministic text similarity (character n-gram shingles).

Good enough to catch 'we published basically this post 30 minutes ago' without
any ML or embedding infrastructure.
"""
from __future__ import annotations

import re

_WS_RE = re.compile(r"\s+")


def _shingles(text: str, n: int = 5) -> set[str]:
    text = _WS_RE.sub(" ", text.lower().strip())
    if len(text) < n:
        return {text}
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def shingle_similarity(a: str, b: str) -> float:
    """Jaccard similarity over 5-char shingles. 0.0..1.0"""
    if not a or not b:
        return 0.0
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
