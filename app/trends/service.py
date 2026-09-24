"""Trend radar service: run providers, normalise into Trend rows, score velocity.

Flow per refresh:
1. RSS (+ optional web search) -> candidate topics
2. Threads search verifies candidates against live discussion
3. Merge hits by normalised topic
4. Sensitivity scan (deterministic) marks sensitive trends
5. Upsert Trend rows, snapshot volumes, recompute velocity
6. Deactivate stale trends (no fresh observation for 48h)
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

import httpx
from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import get_session
from app.models import SystemEvent, Trend, TrendSnapshot
from app.security.prompt_security import scan_trend_sensitivity
from app.trends.providers import TopicHit, TrendProvider, build_providers
from app.trends.scoring import compute_velocity
from app.utils.timeutil import hours_since, now_naive

log = logging.getLogger("trends.service")

_NORMALIZE_RE = re.compile(r"[^\w ]+", flags=re.UNICODE)


def normalize_topic(topic: str) -> str:
    t = _NORMALIZE_RE.sub(" ", topic.lower())
    t = t.replace("_", " ")
    return " ".join(t.split())


class TrendService:
    def __init__(self, settings: Settings | None = None, session_factory=None, providers: list[TrendProvider] | None = None):
        self.s = settings or get_settings()
        self.session_factory = session_factory or get_session
        self.providers = providers or build_providers(self.s)

    async def refresh(self) -> dict:
        """One radar cycle. Returns summary {topics_seen, active, top: [...]}. Safe to call from scheduler."""
        async with httpx.AsyncClient() as client:
            # Pass 1: discovery providers (no candidates needed)
            all_hits: list[TopicHit] = []
            for p in self.providers:
                if p.name in ("news_rss", "web_search", "google_trends"):
                    try:
                        hits = await p.discover(client)
                        all_hits.extend(hits)
                    except Exception as e:
                        log.warning("provider %s failed: %s", p.name, e)
                        self._event("warn", "trends", f"trend provider {p.name} failed: {e}")

            # Candidate keywords for verification providers: top repeated topics
            counter: dict[str, int] = defaultdict(int)
            for h in all_hits:
                counter[normalize_topic(h.topic)] += h.count_hint
            candidates = [t for t, _ in sorted(counter.items(), key=lambda x: -x[1])[:12]]

            # Pass 2: verification providers (Threads search)
            for p in self.providers:
                if p.name in ("threads_search",):
                    try:
                        hits = await p.discover(client, candidates)
                        all_hits.extend(hits)
                    except Exception as e:
                        log.warning("provider %s failed: %s", p.name, e)

        # Merge by normalised topic
        merged: dict[str, dict] = {}
        for h in all_hits:
            key = normalize_topic(h.topic)
            if not key or len(key) < 4:
                continue
            m = merged.setdefault(
                key,
                {
                    "topic": h.topic,
                    "keywords": set(),
                    "sources": set(),
                    "volume": 0,
                    "sentiment": h.sentiment,
                },
            )
            m["keywords"].update(k.lower() for k in h.keywords)
            m["sources"].add(h.source)
            m["volume"] += max(h.count_hint, 1)
            if h.sentiment and h.sentiment != "neutral":
                m["sentiment"] = h.sentiment

        now = now_naive()
        active_count = 0
        with self.session_factory() as db:
            for key, m in merged.items():
                sensitivity = "neutral"
                hits = scan_trend_sensitivity(m["topic"] + " " + " ".join(m["keywords"]))
                if hits:
                    sensitivity = "sensitive:" + ",".join(hits[:2])

                t = db.scalar(select(Trend).where(Trend.topic == key))
                prev_volume = t.discussion_volume if t else None
                if t is None:
                    t = Trend(topic=key)
                    db.add(t)
                    db.flush()
                t.keywords = sorted(m["keywords"])[:10]
                t.sources = sorted(m["sources"])
                t.last_seen = now
                t.discussion_volume = m["volume"]
                t.sentiment = m["sentiment"]
                t.sensitivity = sensitivity
                t.confidence = min(1.0, 0.4 + 0.15 * len(m["sources"]) + (0.2 if m["volume"] > 5 else 0.0))
                t.active = True
                t.velocity = compute_velocity(
                    last_seen=t.last_seen,
                    prev_volume=prev_volume,
                    curr_volume=m["volume"],
                    source_count=len(m["sources"]),
                    keywords=t.keywords,
                    topic=key,
                    now=now,
                )
                db.add(
                    TrendSnapshot(
                        trend_id=t.id,
                        discussion_volume=m["volume"],
                        velocity=t.velocity,
                        source_count=len(m["sources"]),
                    )
                )
                active_count += 1

            # Deactivate stale trends (>48h without observation)
            stale = db.scalars(
                select(Trend).where(Trend.active.is_(True)).where(Trend.last_seen < now)
            ).all()
            for t in stale:
                if hours_since(t.last_seen) > 48:
                    t.active = False

            db.commit()

        top = self.active_trends(limit=5)
        self._event(
            "info",
            "trends",
            f"trend refresh: {len(merged)} topics, {active_count} active, "
            f"top: {', '.join(t['topic'] for t in top)}",
        )
        return {"topics_seen": len(merged), "active": active_count, "top": top}

    def active_trends(self, limit: int = 20, min_velocity: float = 0.0, include_sensitive: bool = False) -> list[dict]:
        with self.session_factory() as db:
            q = select(Trend).where(Trend.active.is_(True))
            rows = db.scalars(q.order_by(Trend.velocity.desc()).limit(limit * 3)).all()
            out = []
            for t in rows:
                if t.velocity < min_velocity:
                    continue
                if not include_sensitive and t.sensitivity.startswith("sensitive"):
                    continue
                out.append(
                    {
                        "id": t.id,
                        "topic": t.topic,
                        "keywords": t.keywords or [],
                        "sources": t.sources or [],
                        "velocity": t.velocity,
                        "volume": t.discussion_volume,
                        "sentiment": t.sentiment,
                        "sensitivity": t.sensitivity,
                        "confidence": t.confidence,
                        "last_seen": t.last_seen,
                    }
                )
                if len(out) >= limit:
                    break
            return out

    def _event(self, level: str, category: str, message: str) -> None:
        try:
            with self.session_factory() as db:
                db.add(SystemEvent(level=level, category=category, message=message[:1000]))
                db.commit()
        except Exception:
            log.exception("failed to log trend event")
