"""Publisher: the gate between 'content ready' and 'on Threads'.

Checks (in order):
1. DRY_RUN / AUTO_PUBLISH switches
2. rate limits: max posts per day, min hours between posts, quiet hours
3. content similarity vs last N published posts
4. disclosure appended if configured
Then publishes via ThreadsClient (or simulates in dry run), stores the
PublishedPost row, and schedules analytics checkpoints.

Idempotency: a product can only produce ONE published post. The state
machine + unique threads_post_id constraint prevent double publishing,
even across restarts.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import get_session
from app.models import (
    AnalyticsCheckpoint,
    ContentCandidate,
    Opportunity,
    Product,
    PublishedPost,
    SystemEvent,
)
from app.publishing.threads_client import ThreadsAPIError, ThreadsClient
from app.state_machine import transition
from app.utils.similarity import shingle_similarity
from app.utils.timeutil import in_quiet_hours, my_now, now_utc

log = logging.getLogger("publishing")


class PublishBlocked(Exception):
    """Non-fatal: publishing is not allowed right now (retry later)."""


class PublishFailed(Exception):
    """Fatal for this attempt (token dead, API error)."""


class Publisher:
    def __init__(self, settings: Settings | None = None, session_factory=None):
        self.s = settings or get_settings()
        self.session_factory = session_factory or get_session

    # ---------- gate checks ----------

    def _posts_today(self) -> int:
        from app.utils.timeutil import ensure_utc

        today = my_now().date()
        with self.session_factory() as db:
            posts = db.scalars(
                select(PublishedPost).where(
                    PublishedPost.status == "published",
                    PublishedPost.dry_run.is_(False),
                )
            ).all()
            return sum(
                1
                for p in posts
                if p.publish_time and ensure_utc(p.publish_time).astimezone(my_now().tzinfo).date() == today
            )

    def _last_publish_time(self):
        with self.session_factory() as db:
            p = db.scalar(
                select(PublishedPost)
                .where(PublishedPost.status == "published", PublishedPost.dry_run.is_(False))
                .order_by(PublishedPost.publish_time.desc())
            )
            return p.publish_time if p else None

    def _recent_post_texts(self, limit: int = 10) -> list[str]:
        with self.session_factory() as db:
            rows = db.scalars(
                select(PublishedPost)
                .where(PublishedPost.status == "published")
                .order_by(PublishedPost.publish_time.desc())
                .limit(limit)
            ).all()
            return [r.content for r in rows if r.content]

    def check_gates(self, content: str) -> None:
        """Raise PublishBlocked if any gate fails. Raises nothing if OK."""
        self.check_rate_gates()
        self.check_similarity(content)

    def check_rate_gates(self) -> None:
        """Time/volume gates. In dry-run these are reported, not blocking."""
        now = my_now()
        if in_quiet_hours(now, self.s.quiet_hours):
            raise PublishBlocked(f"quiet hours ({self.s.quiet_hours})")
        if self._posts_today() >= self.s.max_posts_per_day:
            raise PublishBlocked(f"max posts per day reached ({self.s.max_posts_per_day})")
        last = self._last_publish_time()
        if last is not None:
            gap_h = (now - last).total_seconds() / 3600.0
            if gap_h < self.s.min_hours_between_posts:
                raise PublishBlocked(f"min interval not met ({gap_h:.1f}h < {self.s.min_hours_between_posts}h)")

    def check_similarity(self, content: str) -> None:
        """Content-quality gate. ALWAYS blocks, even in dry run:
        near-duplicate content is bad content, period."""
        for prev in self._recent_post_texts():
            sim = shingle_similarity(content, prev)
            if sim >= self.s.similarity_threshold:
                raise PublishBlocked(f"content too similar to a recent post (similarity {sim:.2f})")

    # ---------- publishing ----------

    async def publish(
        self,
        product: Product,
        candidate: ContentCandidate,
        content: str,
        *,
        opportunity: Opportunity | None = None,
        trend_topic: str | None = None,
    ) -> PublishedPost:
        """Publish (or dry-run) a candidate. Returns the PublishedPost row.

        State transitions: READY_TO_PUBLISH -> PUBLISHING -> PUBLISHED.
        On gate block: stays READY_TO_PUBLISH (retry next tick).
        On API failure: PUBLISHING -> FAILED (or back to READY for retryable).
        """
        # Switches
        if self.s.dry_run:
            mode = "DRY RUN"
        elif not self.s.auto_publish:
            mode = "HOLD (AUTO_PUBLISH=false)"
        else:
            mode = "LIVE"

        # Gates. Rate gates: reported in dry-run, blocking in live.
        # Similarity gate: always blocking (content quality).
        gate_error: str | None = None
        try:
            self.check_rate_gates()
        except PublishBlocked as e:
            gate_error = str(e)
            if not self.s.dry_run:
                raise
        self.check_similarity(content)

        with self.session_factory() as db:
            p = db.get(Product, product.id)
            if p.state != "PUBLISHING":
                transition(p.state, "PUBLISHING")
            p.state = "PUBLISHING"
            db.commit()

        disclosure = self.s.affiliate_disclosure_text if self.s.affiliate_disclosure_enabled else ""
        final_content = content
        if disclosure and disclosure.lower() not in content.lower():
            final_content = f"{content}\n({disclosure})"

        dry_run = self.s.dry_run or not self.s.auto_publish
        post = None
        try:
            if dry_run:
                post_id = f"dryrun-{uuid.uuid4().hex[:12]}"
                permalink = None
                log.info("[DRY RUN] would publish %d chars for product %d", len(final_content), product.id)
            else:
                client = ThreadsClient(self.s)
                result = await client.publish_text(final_content)
                post_id = result["id"]
                permalink = result["permalink"]
                log.info("published to Threads: %s", permalink)

            with self.session_factory() as db:
                p = db.get(Product, product.id)
                # Idempotency: if this product already has a published post, do not duplicate.
                existing = db.scalar(
                    select(PublishedPost).where(
                        PublishedPost.product_id == p.id, PublishedPost.status == "published"
                    )
                )
                if existing is not None:
                    log.warning("product %d already has a published post; skipping duplicate", p.id)
                    if p.state != "PUBLISHED":
                        transition(p.state, "PUBLISHED")
                    p.state = "PUBLISHED"
                    p.state_reason = "duplicate publish prevented"
                    db.commit()
                    db.refresh(existing)
                    return existing

                post = PublishedPost(
                    product_id=p.id,
                    candidate_id=candidate.id if candidate else None,
                    opportunity_id=opportunity.id if opportunity else None,
                    threads_post_id=post_id,
                    threads_permalink=permalink,
                    content=final_content,
                    disclosure=disclosure,
                    trend_id=opportunity.trend_id if opportunity else None,
                    strategy=candidate.angle if candidate else "",
                    publish_time=now_utc(),
                    dry_run=dry_run,
                    status="published",
                )
                db.add(post)
                db.flush()
                self._schedule_checkpoints(db, post.id, now_utc())
                transition(p.state, "PUBLISHED")
                p.state = "PUBLISHED"
                p.state_reason = (
                    f"published ({mode})"
                    + (f" [gate: {gate_error}]" if gate_error else "")
                )
                if candidate is not None:
                    candidate.selected = True
                if opportunity is not None:
                    opportunity.status = "used"
                db.add(
                    SystemEvent(
                        level="info",
                        category="publish",
                        message=f"post published for product {p.id} ({mode}): {post_id}",
                        detail={"gate": gate_error, "trend": trend_topic},
                    )
                )
                db.commit()
                db.refresh(post)
                return post
        except ThreadsAPIError as e:
            with self.session_factory() as db:
                p = db.get(Product, product.id)
                if e.is_auth_error:
                    p.state = "FAILED"
                    p.state_reason = "Threads auth error — token may be expired"
                    db.add(SystemEvent(level="error", category="publish", message=f"Threads auth error: {e}"))
                else:
                    # retryable: back to READY_TO_PUBLISH
                    p.state = "READY_TO_PUBLISH"
                    p.state_reason = f"publish failed, will retry: {str(e)[:200]}"
                    db.add(SystemEvent(level="warn", category="publish", message=f"publish failed: {e}"))
                db.commit()
            raise PublishFailed(str(e)) from e

    def _schedule_checkpoints(self, db, post_id: int, publish_time) -> None:
        for h in self.s.checkpoints_hours:
            due = publish_time + timedelta(hours=h)
            db.add(
                AnalyticsCheckpoint(
                    post_id=post_id,
                    checkpoint=f"{h:g}h",
                    due_at=due,
                )
            )
