"""Trend providers.

TrendProvider (interface)
  -> RSSNewsProvider       (Malaysian news feeds, free, no key)
  -> ThreadsSearchProvider (Threads API keyword search for candidate topics)
  -> WebSearchProvider     (optional search API, off unless key configured)
  -> GoogleTrendsProvider  (optional, off by default, unofficial)

Each provider returns raw topic hits: {topic, keywords, source, url?, count_hint}
The TrendService normalises, dedupes, and scores them.
Providers fail soft: an exception in one never kills the radar.
"""
from __future__ import annotations

import abc
import asyncio
import logging
import re
from dataclasses import dataclass, field

import httpx

from app.config import Settings, get_settings

log = logging.getLogger("trends.providers")


@dataclass
class TopicHit:
    topic: str
    keywords: list[str] = field(default_factory=list)
    source: str = ""
    url: str = ""
    count_hint: int = 1  # how many times this topic appeared in this provider's batch
    sentiment: str = ""
    sensitivity: str = "neutral"


class TrendProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def discover(self, client: httpx.AsyncClient, candidate_keywords: list[str] | None = None) -> list[TopicHit]:
        """Return topic hits. candidate_keywords (from other providers) may be
        used to verify discussion on this platform."""


# ---------------------------------------------------------------- RSS news

DEFAULT_RSS_FEEDS = [
    ("https://www.nst.com.my/rss", "NST"),
    ("https://www.malaysiakini.com/feed", "Malaysiakini"),
    ("https://www.bh.com.my/feed", "BH"),
    ("https://www.astroawani.com/rss", "Astro Awani"),
    ("https://www.bernama.com/rss/", "Bernama"),
]

_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ]{3,}")
_STOPWORDS = {
    "dan", "atau", "untuk", "dengan", "pada", "dari", "ini", "itu",
    "the", "and", "for", "with", "from", "this", "that", "are", "was", "were",
    "will", "has", "have", "had", "not", "but", "all", "its", "you", "your",
    "melayu", "bahasa", "kementerian", "menteri", "today", "today's",
}


def extract_topics(titles: list[str], min_len: int = 3) -> list[TopicHit]:
    """Very lightweight topic extraction: phrase mining over headlines.

    Deterministic and cheap. We look for repeated significant phrases
    (2-4 word n-grams) across headlines; repetition = a topic is forming.
    """
    from collections import Counter

    ngram_counts: Counter = Counter()
    ngram_examples: dict = {}
    for title in titles:
        words = [w.lower() for w in _WORD_RE.findall(title)]
        words = [w for w in words if w not in _STOPWORDS and len(w) >= min_len]
        if not words:
            continue
        seen_in_title: set = set()
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                gram = " ".join(words[i : i + n])
                if gram in seen_in_title:
                    continue
                seen_in_title.add(gram)
                ngram_counts[gram] += 1
                ngram_examples.setdefault(gram, title)

    hits = []
    for gram, count in ngram_counts.most_common(40):
        if count < 2:
            continue
        hits.append(
            TopicHit(
                topic=gram.title(),
                keywords=gram.split(),
                source="news_rss",
                count_hint=count,
            )
        )
    return hits


class RSSNewsProvider(TrendProvider):
    name = "news_rss"

    def __init__(self, settings: Settings | None = None, feeds: list[tuple[str, str]] | None = None):
        self.s = settings or get_settings()
        self.feeds = feeds or DEFAULT_RSS_FEEDS

    async def discover(self, client: httpx.AsyncClient, candidate_keywords: list[str] | None = None) -> list[TopicHit]:
        import feedparser  # local import: keeps module importable without it

        titles: list[str] = []
        sources: set[str] = set()

        async def fetch_one(url: str, label: str):
            try:
                resp = await client.get(url, timeout=12.0)
                if resp.status_code != 200:
                    return
                parsed = feedparser.parse(resp.content)
                for entry in parsed.entries[:30]:
                    t = (entry.get("title") or "").strip()
                    if t:
                        titles.append(t)
                        sources.add(label)
            except Exception as e:
                log.warning("RSS feed %s failed: %s", label, e)

        await asyncio.gather(*(fetch_one(u, l) for u, l in self.feeds))
        if not titles:
            return []
        hits = extract_topics(titles)
        for h in hits:
            h.sources_note = f"{len(sources)} feeds"  # type: ignore[attr-defined]
        log.info("RSS provider: %d titles -> %d topic hits", len(titles), len(hits))
        return hits


# --------------------------------------------------------- Threads search

class ThreadsSearchProvider(TrendProvider):
    """Verify candidate topics against live Threads discussion via the
    official threads_search endpoint. Requires a valid access token.
    Returns hits with real discussion volume from the API.
    """

    name = "threads_search"

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return bool(self.s.threads_access_token)

    async def discover(self, client: httpx.AsyncClient, candidate_keywords: list[str] | None = None) -> list[TopicHit]:
        if not self.enabled or not candidate_keywords:
            return []
        from app.publishing.threads_client import ThreadsClient, ThreadsAPIError

        tclient = ThreadsClient(self.s)
        hits: list[TopicHit] = []
        seen: set[str] = set()
        for kw in candidate_keywords[:12]:
            if kw in seen:
                continue
            seen.add(kw)
            try:
                data = await tclient.search_threads(kw, ordering="top")
            except ThreadsAPIError as e:
                log.warning("threads search failed for %r: %s", kw, e)
                continue
            except httpx.HTTPError as e:
                log.warning("threads search network error for %r: %s", kw, e)
                continue
            posts = data.get("data", []) if isinstance(data, dict) else []
            if not posts:
                continue
            volume = len(posts)
            # Estimate engagement from visible likes counts.
            likes = 0
            for p in posts[:10]:
                try:
                    likes += int(p.get("like_count", 0))
                except (TypeError, ValueError):
                    pass
            hits.append(
                TopicHit(
                    topic=kw.title(),
                    keywords=[kw],
                    source="threads_search",
                    count_hint=volume,
                    sentiment="neutral",
                )
            )
            log.info("threads search %r: %d posts, ~%d likes in top10", kw, volume, likes)
        return hits


# ------------------------------------------------------------- web search

class WebSearchProvider(TrendProvider):
    """Optional: Brave Search API (free tier available). Off unless configured."""

    name = "web_search"

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self.s.search_api_provider == "brave" and bool(self.s.search_api_key)

    async def discover(self, client: httpx.AsyncClient, candidate_keywords: list[str] | None = None) -> list[TopicHit]:
        if not self.enabled:
            return []
        queries = candidate_keywords or ["malaysia trending today"]
        hits: list[TopicHit] = []
        for q in queries[:8]:
            try:
                resp = await client.get(
                    "https://api.search.brave.com/res/v1/news/search",
                    params={"q": f"{q} malaysia", "count": 10},
                    headers={"X-Subscription-Token": self.s.search_api_key, "Accept": "application/json"},
                    timeout=12.0,
                )
                if resp.status_code != 200:
                    continue
                results = (resp.json().get("results") or [])
                titles = [r.get("title", "") for r in results]
                hits.extend(extract_topics(titles))
            except Exception as e:
                log.warning("brave search failed for %r: %s", q, e)
        return hits


# ---------------------------------------------------------- google trends

class GoogleTrendsProvider(TrendProvider):
    """UNOFFICIAL (pytrends). Off by default; fragile and can get IP-blocked."""

    name = "google_trends"

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self.s.google_trends_enabled

    async def discover(self, client: httpx.AsyncClient, candidate_keywords: list[str] | None = None) -> list[TopicHit]:
        if not self.enabled:
            return []
        try:
            import pytrends

            def _run():
                pt = pytrends.PyTrends(timeout=(10, 30))
                pt.build_payload(["malaysia"], timeframe="today 5-h", geo="")
                df = pt.interest_over_time()
                if df is None or df.empty:
                    return []
                df = df.drop(columns=["isPartial"], errors="ignore")
                return df.mean().sort_values(ascending=False).head(10).to_dict()

            loop = asyncio.get_running_loop()
            top = await loop.run_in_executor(None, _run)
            return [
                TopicHit(topic=k.title(), keywords=[k.lower()], source="google_trends", count_hint=int(v))
                for k, v in top.items()
            ]
        except Exception as e:
            log.warning("google trends provider failed (expected, unofficial): %s", e)
            return []


def build_providers(settings: Settings | None = None) -> list[TrendProvider]:
    s = settings or get_settings()
    return [
        RSSNewsProvider(s),
        ThreadsSearchProvider(s),
        WebSearchProvider(s),
        GoogleTrendsProvider(s),
    ]
