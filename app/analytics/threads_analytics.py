"""Threads analytics: fetch per-post insights at checkpoints, store snapshots.

Metric names are requested broadly and whatever the API returns is stored
(as-is in `raw`, mapped into typed columns when the name matches). We do NOT
assume a fixed metric set — the API evolves.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import get_session
from app.models import AnalyticsCheckpoint, PostMetricSnapshot, PublishedPost, SystemEvent
from app.publishing.threads_client import ThreadsAPIError, ThreadsClient
from app.utils.timeutil import now_naive, now_utc

log = logging.getLogger("analytics")

# Map of API metric names -> our columns. Unknown names stay in `raw`.
METRIC_MAP = {
    "likes_count": "likes",
    "replies_count": "replies",
    "reposts_count": "reposts",
    "shares_count": "shares",
    "external_action_count": "external_actions",
    "views": "views",
    "quote_count": "quotes",
}


def parse_insights(raw: dict) -> dict:
    """Convert a /insights response into our column dict.

    Shape: {"data": [{"name": "likes_count", "value": 42, ...}, ...]}
    Some metrics may be missing or the whole call may 400 if the metric
    is unavailable for the account — we keep what we can.
    """
    out: dict = {}
    data = raw.get("data") or []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if name is None or value is None:
            continue
        col = METRIC_MAP.get(name)
        if col:
            try:
                out[col] = int(value)
            except (TypeError, ValueError):
                out[col] = None
    return out


class AnalyticsService:
    def __init__(self, settings: Settings | None = None, session_factory=None):
        self.s = settings or get_settings()
        self.session_factory = session_factory or get_session

    async def collect(self, post_id: int, checkpoint: str) -> PostMetricSnapshot | None:
        """Collect one checkpoint for one post. Returns snapshot or None."""
        with self.session_factory() as db:
            post = db.get(PublishedPost, post_id)
            if post is None or not post.threads_post_id or post.threads_post_id.startswith("dryrun-"):
                # Dry-run posts have no real metrics; mark checkpoint done.
                self._mark_checkpoint(post_id, checkpoint)
                return None
            threads_post_id = post.threads_post_id

        try:
            client = ThreadsClient(self.s)
            raw = await client.post_insights(threads_post_id)
        except ThreadsAPIError as e:
            log.warning("insights failed for %s (%s): %s", threads_post_id, checkpoint, e)
            self._mark_checkpoint(post_id, checkpoint, error=str(e)[:300])
            return None
        except Exception as e:
            log.warning("insights unexpected error: %s", e)
            self._mark_checkpoint(post_id, checkpoint, error=str(e)[:300])
            return None

        metrics = parse_insights(raw)
        with self.session_factory() as db:
            snap = PostMetricSnapshot(
                post_id=post_id,
                checkpoint=checkpoint,
                views=metrics.get("views"),
                likes=metrics.get("likes"),
                replies=metrics.get("replies"),
                reposts=metrics.get("reposts"),
                quotes=metrics.get("quotes"),
                shares=metrics.get("shares"),
                external_actions=metrics.get("external_actions"),
                raw=raw,
            )
            # Upsert: never overwrite an earlier snapshot of the same checkpoint.
            existing = db.scalar(
                select(PostMetricSnapshot).where(
                    PostMetricSnapshot.post_id == post_id,
                    PostMetricSnapshot.checkpoint == checkpoint,
                )
            )
            if existing is not None:
                for k, v in metrics.items():
                    setattr(existing, k, v)
                existing.raw = raw
                existing.captured_at = now_utc()
            else:
                db.add(snap)
            self._mark_checkpoint_row(db, post_id, checkpoint)
            db.commit()
            db.refresh(snap if existing is None else existing)
            return snap

    def _mark_checkpoint(self, post_id: int, checkpoint: str, error: str = "") -> None:
        with self.session_factory() as db:
            self._mark_checkpoint_row(db, post_id, checkpoint, error=error)
            db.commit()

    def _mark_checkpoint_row(self, db, post_id: int, checkpoint: str, error: str = "") -> None:
        row = db.scalar(
            select(AnalyticsCheckpoint).where(
                AnalyticsCheckpoint.post_id == post_id,
                AnalyticsCheckpoint.checkpoint == checkpoint,
            )
        )
        if row is not None:
            row.done = True
            row.done_at = now_utc()
            row.error = error[:500]

    async def process_due(self) -> int:
        """Process all due checkpoints. Returns count collected."""
        now = now_naive()
        with self.session_factory() as db:
            due = db.scalars(
                select(AnalyticsCheckpoint).where(
                    AnalyticsCheckpoint.done.is_(False),
                    AnalyticsCheckpoint.due_at <= now,
                )
            ).all()
            jobs = [(c.post_id, c.checkpoint) for c in due]

        collected = 0
        for post_id, checkpoint in jobs:
            snap = await self.collect(post_id, checkpoint)
            if snap is not None:
                collected += 1
        return collected

    def all_complete_for_post(self, post_id: int) -> bool:
        with self.session_factory() as db:
            rows = db.scalars(
                select(AnalyticsCheckpoint).where(AnalyticsCheckpoint.post_id == post_id)
            ).all()
            return all(r.done for r in rows) if rows else True

    def notify_24h(self, post_id: int) -> str | None:
        """Build a human summary for the 24h checkpoint (for Telegram)."""
        with self.session_factory() as db:
            post = db.get(PublishedPost, post_id)
            snap = db.scalar(
                select(PostMetricSnapshot)
                .where(PostMetricSnapshot.post_id == post_id, PostMetricSnapshot.checkpoint == "24h")
                .order_by(PostMetricSnapshot.captured_at.desc())
            )
            if post is None or snap is None:
                return None
            lines = [f"📊 24h: {post.content[:80]}…"]
            if snap.views is not None:
                lines.append(f"views: {snap.views:,}")
            if snap.likes is not None:
                lines.append(f"likes: {snap.likes:,}")
            if snap.replies is not None:
                lines.append(f"replies: {snap.replies:,}")
            if snap.external_actions is not None:
                lines.append(f"link clicks: {snap.external_actions:,}")
            return "\n".join(lines)
