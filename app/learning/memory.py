"""Strategy memory: SQL-based performance memory (no ML in V1).

Two parts:
1. retrieve_notes(product, intelligence) -> text block fed into the
   strategist prompt. Pulls top historical patterns by category/angle/hook.
2. refresh() -> aggregates published posts + snapshots into StrategyMemory
   rows (run daily). Answers: which categories/angles/hooks/time windows work.

Design for future upgrade: the aggregation queries here are exactly the
features a contextual bandit would consume later.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import get_session
from app.models import (
    LlmUsage,
    PostMetricSnapshot,
    Product,
    PublishedPost,
    StrategyMemory,
)
from app.schemas import ProductIntelligence
from app.utils.timeutil import my_now, now_naive, now_utc, to_tz

log = logging.getLogger("learning")


def _engagement_of(snap) -> float:
    """Engagement rate proxy: (likes+replies+reposts+shares+external)/views."""
    if snap is None or not snap.views:
        return 0.0
    num = sum(
        getattr(snap, k) or 0
        for k in ("likes", "replies", "reposts", "shares", "external_actions")
    )
    return num / max(snap.views, 1)


def latest_snapshot(db, post_id: int):
    return db.scalar(
        select(PostMetricSnapshot)
        .where(PostMetricSnapshot.post_id == post_id)
        .order_by(PostMetricSnapshot.captured_at.desc())
    )


class StrategyMemoryService:
    def __init__(self, settings: Settings | None = None, session_factory=None):
        self.s = settings or get_settings()
        self.session_factory = session_factory or get_session

    # ---------- retrieval (feeds the strategist prompt) ----------

    def retrieve_notes(self, product: Product, intelligence: ProductIntelligence | None = None) -> str:
        """Build the 'what worked before' evidence block. Empty string if no history."""
        with self.session_factory() as db:
            rows = db.scalars(
                select(StrategyMemory).where(StrategyMemory.posts_count >= 2)
            ).all()
            if not rows:
                return ""

            lines = ["Historical performance notes (evidence of what worked, do NOT copy posts):"]
            category = (product.category or (intelligence.category if intelligence else "")).lower()
            audience = (
                (intelligence.target_audiences[0].lower() if intelligence and intelligence.target_audiences else "")
            )
            for r in rows:
                p = r.payload or {}
                if r.kind == "category" and category and r.key.lower() == category:
                    lines.append(
                        f"- category '{r.key}': avg engagement {p.get('avg_engagement', 0):.1%} "
                        f"across {p.get('posts', r.posts_count)} posts; best angle: {p.get('best_angle', '?')}"
                    )
                elif r.kind == "angle" and p.get("avg_engagement", 0) > 0.02:
                    lines.append(
                        f"- angle '{r.key}': avg engagement {p.get('avg_engagement', 0):.1%} ({p.get('posts', 0)} posts)"
                    )
                elif r.kind == "hook" and p.get("avg_engagement", 0) > 0.02:
                    lines.append(
                        f"- hook '{r.key}': avg engagement {p.get('avg_engagement', 0):.1%} ({p.get('posts', 0)} posts)"
                    )
                elif r.kind == "time_window":
                    lines.append(f"- posting window {r.key}: avg engagement {p.get('avg_engagement', 0):.1%}")
                elif r.kind == "audience" and audience and audience in r.key.lower():
                    lines.append(
                        f"- audience '{r.key}': avg engagement {p.get('avg_engagement', 0):.1%} ({p.get('posts', 0)} posts)"
                    )
            if len(lines) == 1:
                return ""
            return "\n".join(lines[:12])

    # ---------- aggregation (daily refresh) ----------

    def refresh(self, days: int = 30) -> int:
        """Recompute strategy memory from the last N days of posts. Returns rows written."""
        cutoff = now_naive() - timedelta(days=days)
        with self.session_factory() as db:
            posts = db.scalars(
                select(PublishedPost).where(
                    PublishedPost.status == "published",
                    PublishedPost.publish_time >= cutoff,
                )
            ).all()
            if not posts:
                return 0

            def agg(key: str, kind: str, posts_subset: list[PublishedPost], extra: dict | None = None) -> dict | None:
                if len(posts_subset) < 2:
                    return None
                eng = []
                for p in posts_subset:
                    snap = latest_snapshot(db, p.id)
                    eng.append(_engagement_of(snap))
                avg = sum(eng) / len(eng)
                best_angle = max(
                    posts_subset, key=lambda p: _engagement_of(latest_snapshot(db, p.id))
                ).strategy
                payload = {
                    "avg_engagement": round(avg, 4),
                    "posts": len(posts_subset),
                    "best_angle": best_angle,
                }
                if extra:
                    payload.update(extra)
                return {"key": key, "kind": kind, "payload": payload, "count": len(posts_subset)}

            rows_to_write: list[dict] = []

            # By category
            by_cat: dict[str, list[PublishedPost]] = {}
            for p in posts:
                prod = db.get(Product, p.product_id)
                cat = (prod.category if prod else "") or "unknown"
                by_cat.setdefault(cat, []).append(p)
            for cat, subset in by_cat.items():
                r = agg(cat, "category", subset)
                if r:
                    rows_to_write.append(r)

            # By angle (strategy)
            by_angle: dict[str, list[PublishedPost]] = {}
            for p in posts:
                by_angle.setdefault(p.strategy or "unknown", []).append(p)
            for angle, subset in by_angle.items():
                r = agg(angle, "angle", subset)
                if r:
                    rows_to_write.append(r)

            # By hook (from candidate via post.candidate_id -> not stored on post;
            # use strategy as proxy + content heuristics)
            # By time window (MYT 3h buckets)
            by_hour: dict[str, list[PublishedPost]] = {}
            for p in posts:
                if not p.publish_time:
                    continue
                h = to_tz(p.publish_time).hour
                bucket = f"{(h // 3) * 3:02d}:00-{((h // 3) * 3 + 2):02d}:00"
                by_hour.setdefault(bucket, []).append(p)
            for bucket, subset in by_hour.items():
                r = agg(bucket, "time_window", subset)
                if r:
                    rows_to_write.append(r)

            # By audience (first audience of the product's intelligence)
            by_aud: dict[str, list[PublishedPost]] = {}
            for p in posts:
                prod = db.get(Product, p.product_id)
                if prod and prod.analysis:
                    auds = (prod.analysis or {}).get("target_audiences") or []
                    if auds:
                        by_aud.setdefault(auds[0].lower(), []).append(p)
            for aud, subset in by_aud.items():
                r = agg(aud, "audience", subset)
                if r:
                    rows_to_write.append(r)

            for r in rows_to_write:
                existing = db.scalar(select(StrategyMemory).where(StrategyMemory.key == r["key"]))
                if existing is None:
                    db.add(
                        StrategyMemory(
                            key=r["key"], kind=r["kind"], payload=r["payload"], posts_count=r["count"]
                        )
                    )
                else:
                    existing.kind = r["kind"]
                    existing.payload = r["payload"]
                    existing.posts_count = r["count"]
            db.commit()
            log.info("strategy memory refreshed: %d rows from %d posts", len(rows_to_write), len(posts))
            return len(rows_to_write)

    # ---------- stats for /stats ----------

    def stats_summary(self, days: int = 7) -> dict:
        cutoff = now_naive() - timedelta(days=days)
        with self.session_factory() as db:
            posts = db.scalars(
                select(PublishedPost).where(
                    PublishedPost.status == "published",
                    PublishedPost.publish_time >= cutoff,
                )
            ).all()
            submitted = db.scalars(
                select(Product).where(Product.submitted_at >= cutoff)
            ).all()

            total_views = 0
            engs = []
            for p in posts:
                snap = latest_snapshot(db, p.id)
                if snap and snap.views:
                    total_views += snap.views
                engs.append(_engagement_of(snap))
            avg_eng = (sum(engs) / len(engs)) if engs else 0.0

            # Best category / angle / window from strategy memory
            def best_of(kind: str) -> tuple[str, float] | None:
                rows = db.scalars(select(StrategyMemory).where(StrategyMemory.kind == kind)).all()
                if not rows:
                    return None
                top = max(rows, key=lambda r: (r.payload or {}).get("avg_engagement", 0))
                return top.key, (top.payload or {}).get("avg_engagement", 0)

            return {
                "days": days,
                "products_submitted": len(submitted),
                "posts_published": len(posts),
                "total_views": total_views,
                "avg_engagement": avg_eng,
                "best_category": best_of("category"),
                "best_angle": best_of("angle"),
                "best_window": best_of("time_window"),
                "best_audience": best_of("audience"),
            }
