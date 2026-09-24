"""Pipeline orchestrator: the heart of the engine.

Drives products through the state machine:

  ANALYZED
    -> (instant mode)  generate -> review -> publish
    -> (smart mode)    QUEUED -> WAITING_FOR_OPPORTUNITY
         -> opportunity scan finds a trend >= threshold  -> generate -> review -> publish
         -> max wait elapsed                              -> evergreen -> generate -> review -> publish

LLM budget per full cycle: 4 calls (intelligence, trend match, candidates, critic).
Deterministic work (scoring, gating, scheduling) never touches the LLM.

The orchestrator is restart-safe: every step re-reads state from the DB and
only acts on products in the states it owns. Idempotent by construction.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from app.analytics.threads_analytics import AnalyticsService
from app.config import Settings, get_settings
from app.database import get_session
from app.intelligence.critic import critique
from app.intelligence.product_analyst import analyze_product
from app.intelligence.strategist import generate_candidates
from app.intelligence.trend_matcher import match_trends
from app.learning.memory import StrategyMemoryService
from app.llm.client import LLMClient
from app.models import ContentCandidate, Opportunity, Product, SystemEvent, Trend
from app.opportunity.engine import best_opportunity, evergreen_opportunity
from app.product.service import ProductService
from app.publishing.publisher import PublishBlocked, PublishFailed, Publisher
from app.schemas import ProductIntelligence
from app.state_machine import is_terminal
from app.trends.service import TrendService
from app.utils.timeutil import hours_since, now_utc

log = logging.getLogger("pipeline")


class Pipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        session_factory=None,
        products: ProductService | None = None,
        trends: TrendService | None = None,
        publisher: Publisher | None = None,
        analytics: AnalyticsService | None = None,
        memory: StrategyMemoryService | None = None,
        llm: LLMClient | None = None,
        notifier=None,  # async callable(level, text) for Telegram
    ):
        self.s = settings or get_settings()
        self.session_factory = session_factory or get_session
        self.products = products or ProductService(self.s, self.session_factory)
        self.trends = trends or TrendService(self.s, self.session_factory)
        self.publisher = publisher or Publisher(self.s, self.session_factory)
        self.analytics = analytics or AnalyticsService(self.s, self.session_factory)
        self.memory = memory or StrategyMemoryService(self.s, self.session_factory)
        self.llm = llm or LLMClient(self.s, session_factory=self.session_factory)
        self.notifier = notifier
        self.paused = False

    async def _notify(self, level: str, text: str) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier(level, text)
        except Exception as e:
            log.warning("notifier failed: %s", e)

    # ==================================================================
    # ENTRY POINTS (called by scheduler / telegram)
    # ==================================================================

    async def intake(self, product_id: int) -> None:
        """RECEIVED -> RESOLVING -> ANALYZED. Safe to call repeatedly.

        Also resumes products stuck in RESOLVING after a restart.
        """
        with self.session_factory() as db:
            p = db.get(Product, product_id)
            if p is None or is_terminal(p.state):
                return
            if p.state not in ("RECEIVED", "RESOLVING"):
                return
        await self.products.resolve_product(product_id)
        await self._analyze(product_id)

    async def _analyze(self, product_id: int) -> None:
        """RESOLVING/ANALYZED: run product intelligence (LLM call 1)."""
        with self.session_factory() as db:
            p = db.get(Product, product_id)
            if p is None or is_terminal(p.state):
                return
            if p.state not in ("RESOLVING", "ANALYZED"):
                return
        try:
            with self.session_factory() as db:
                p = db.get(Product, product_id)
                intelligence = await analyze_product(p, llm=self.llm)
                p.analysis = intelligence.model_dump()
                p.analyzed_at = now_utc()
                # Backfill category from intelligence if we didn't get one.
                if not p.category and intelligence.category:
                    p.category = intelligence.category
                db.commit()
        except Exception as e:
            log.exception("analysis failed for product %d", product_id)
            self.products._fail(product_id, f"analysis failed: {e}")
            await self._notify("warn", f"⚠️ Could not analyze product #{product_id}: {str(e)[:120]}")
            return

        await self._notify(
            "info",
            f"🧠 Product analyzed: {self.products.get(product_id).title[:60]}",
        )

        with self.session_factory() as db:
            p = db.get(Product, product_id)
            if p.mode == "instant":
                self.products.set_state(product_id, "GENERATING", "instant mode")
                await self._generate_and_publish(product_id)
            else:
                self.products.set_state(product_id, "QUEUED", "smart auto queue")
                self.products.set_state(product_id, "WAITING_FOR_OPPORTUNITY", "waiting for trend or max wait")
                await self._notify(
                    "info",
                    f"✅ Product detected\n\nProduct:\n{p.title[:80]}\n"
                    f"Price:\n{('RM' + format(p.price, '.2f')) if p.price else 'n/a'}\n"
                    f"Audience:\n{', '.join((p.analysis or {}).get('target_audiences', [])[:4])}\n"
                    f"Status:\nAdded to Smart Auto Queue",
                )

    # ==================================================================
    # OPPORTUNITY SCAN (smart mode)
    # ==================================================================

    async def scan_opportunities(self) -> int:
        """For each WAITING product: match trends, decide go/evergreen/wait.

        Returns number of products advanced.
        """
        if self.paused:
            return 0
        advanced = 0
        waiting = self.products.list_by_state("WAITING_FOR_OPPORTUNITY")
        if not waiting:
            return 0

        active_trends = self.trends.active_trends(limit=15)
        now = now_utc()

        for p in waiting:
            with self.session_factory() as db:
                db_p = db.get(Product, p.id)
                intelligence = ProductIntelligence.model_validate(db_p.analysis or {})
                # Load trend objects for the active trend ids
                trend_objs = db.scalars(select(Trend).where(Trend.id.in_([t["id"] for t in active_trends]) if active_trends else [0])).all()

            # Max wait check (evergreen fallback)
            waited_h = hours_since(p.submitted_at)
            force_evergreen = waited_h >= self.s.smart_max_wait_hours

            if not active_trends or self.s.content_strategy == "evergreen":
                self._advance_to_generate(p.id, evergreen_opportunity(), db_p)
                advanced += 1
                continue

            try:
                matches = await match_trends(db_p, intelligence, list(trend_objs), llm=self.llm)
            except Exception as e:
                log.warning("trend match failed for %d: %s", p.id, e)
                # On match failure, don't burn the product: retry next scan.
                continue

            best = best_opportunity(matches, self.s.opportunity_threshold, now)
            if best is not None:
                log.info(
                    "product %d matched trend %r (score %s)",
                    p.id, best["topic"], best["score"],
                )
                await self._notify(
                    "info",
                    f"🔥 Strong trend match found\n\nProduct: {p.title[:60]}\nTrend: {best['topic']}\nScore: {best['score']:.0f}/100",
                )
                self._advance_to_generate(p.id, best, db_p)
            elif force_evergreen:
                log.info("product %d hit max wait (%.0fh); going evergreen", p.id, waited_h)
                self._advance_to_generate(p.id, evergreen_opportunity(), db_p)
            # else: keep waiting for a trend
            advanced += 1 if (best is not None or force_evergreen) else 0

        return advanced

    def _advance_to_generate(self, product_id: int, opp: dict, p: Product) -> None:
        with self.session_factory() as db:
            row = db.get(Product, product_id)
            if opp.get("trend") is not None:
                db.add(
                    Opportunity(
                        product_id=row.id,
                        trend_id=opp["trend"].id,
                        kind="trend",
                        score=opp.get("score", 0.0),
                        breakdown=opp.get("breakdown", {}),
                    )
                )
            else:
                db.add(
                    Opportunity(
                        product_id=row.id,
                        trend_id=None,
                        kind="evergreen",
                        score=0.0,
                        breakdown=opp.get("breakdown", {}),
                    )
                )
            db.commit()
        self.products.set_state(product_id, "GENERATING", f"opportunity: {opp.get('topic') or 'evergreen'}")
        import asyncio

        asyncio.create_task(self._generate_and_publish(product_id))

    # ==================================================================
    # GENERATE -> REVIEW -> PUBLISH
    # ==================================================================

    async def _generate_and_publish(self, product_id: int) -> None:
        """GENERATING -> REVIEWING -> READY_TO_PUBLISH -> PUBLISHING -> PUBLISHED."""
        with self.session_factory() as db:
            p = db.get(Product, product_id)
            if p is None or is_terminal(p.state) or p.state != "GENERATING":
                return
            intelligence = ProductIntelligence.model_validate(p.analysis or {})
            # Find the open opportunity for this product
            opp = db.scalar(
                select(Opportunity)
                .where(Opportunity.product_id == p.id, Opportunity.status == "open")
                .order_by(Opportunity.id.desc())
            )
            trend_dict = None
            if opp is not None and opp.trend_id is not None:
                t = db.get(Trend, opp.trend_id)
                if t is not None:
                    trend_dict = {
                        "topic": t.topic,
                        "keywords": t.keywords or [],
                        "velocity": t.velocity,
                        "sentiment": t.sentiment,
                        "angle_hint": (opp.breakdown or {}).get("angle_hint", ""),
                    }
            posts_today = self.publisher._posts_today()

        try:
            strategy_notes = self.memory.retrieve_notes(p, intelligence)
            candidates = await generate_candidates(
                p, intelligence, trend_dict, strategy_notes, posts_today=posts_today, llm=self.llm
            )
        except Exception as e:
            log.exception("candidate generation failed for %d", product_id)
            self.products._fail(product_id, f"generation failed: {e}")
            await self._notify("warn", f"⚠️ Content generation failed for #{product_id}. Will not retry automatically.")
            return

        # Persist candidates
        with self.session_factory() as db:
            opp_row = db.scalar(
                select(Opportunity)
                .where(Opportunity.product_id == product_id, Opportunity.status == "open")
                .order_by(Opportunity.id.desc())
            )
            cand_rows = []
            for c in candidates:
                row = ContentCandidate(
                    product_id=product_id,
                    opportunity_id=opp_row.id if opp_row else None,
                    angle=c.get("angle", ""),
                    hook_type=c.get("hook_type", ""),
                    content=c.get("content", ""),
                )
                db.add(row)
                cand_rows.append(row)
            db.commit()
            for r in cand_rows:
                db.refresh(r)

        self.products.set_state(product_id, "REVIEWING", f"{len(candidates)} candidates generated")
        await self._notify("info", f"✍️ Content prepared for {p.title[:50]} ({len(candidates)} candidates)")

        # Critic (LLM call 4)
        try:
            result = await critique(p, intelligence, candidates, trend_dict, llm=self.llm)
        except Exception as e:
            log.exception("critic failed for %d", product_id)
            self.products._fail(product_id, f"critic failed: {e}")
            return

        with self.session_factory() as db:
            for i, c in enumerate(candidates):
                row = db.get(ContentCandidate, cand_rows[i].id)
                verdict = next(
                    (v for v in result.report.verdicts if v.candidate_index == i), None
                )
                if verdict is not None:
                    row.critic = verdict.model_dump()
                if i in result.vetoed:
                    row.rejected_reason = "deterministic guard veto"
            db.commit()

        if not result.publish or result.winner_content is None:
            self.products.set_state(product_id, "SKIPPED", "critic rejected all candidates")
            await self._notify(
                "warn",
                f"⚠️ No safe post passed review for {p.title[:50]}. "
                f"Skipped. Reason: {result.report.notes[:120] or 'critic rejected all'}",
            )
            return

        # Mark winner, move to READY_TO_PUBLISH
        winner_row = cand_rows[result.winner_index]
        with self.session_factory() as db:
            row = db.get(ContentCandidate, winner_row.id)
            row.selected = True
            if result.winner_content != row.content:
                row.content = result.winner_content
            db.commit()
            db.refresh(row)

        self.products.set_state(product_id, "READY_TO_PUBLISH", f"winner: {winner_row.angle}")

        # Publish (gates may block -> stays READY_TO_PUBLISH for next tick)
        try:
            with self.session_factory() as db:
                opp_row = db.scalar(
                    select(Opportunity)
                    .where(Opportunity.product_id == product_id, Opportunity.status == "open")
                    .order_by(Opportunity.id.desc())
                )
                if opp_row is not None:
                    db.expunge(opp_row)
            post = await self.publisher.publish(
                p,
                winner_row,
                result.winner_content,
                opportunity=opp_row,
                trend_topic=(trend_dict or {}).get("topic"),
            )
        except PublishBlocked as e:
            # Stays READY_TO_PUBLISH; pipeline tick retries later.
            log.info("publish blocked for %d: %s", product_id, e)
            return
        except PublishFailed as e:
            await self._notify("error", f"⚠️ Threads publish failed: {str(e)[:150]}. Check token/settings.")
            return

        # Post published: schedule analytics + transition
        self._post_published(product_id, post)

    def _post_published(self, product_id: int, post) -> None:
        with self.session_factory() as db:
            p = db.get(Product, product_id)
            if p.state == "PUBLISHED":
                if post.dry_run:
                    self.products.set_state(product_id, "COMPLETED", "dry run complete")
                else:
                    self.products.set_state(product_id, "ANALYTICS_PENDING", "checkpoints scheduled")
        if post.dry_run:
            await_notify = self._notify(
                "info",
                f"[DRY RUN]\n\nCandidate selected:\n{post.content[:300]}\n\nWould publish to Threads.\n"
                f"(Set DRY_RUN=false and AUTO_PUBLISH=true to go live.)",
            )
        else:
            await_notify = self._notify(
                "info",
                f"🚀 Published\n\n{post.content[:200]}\n\n{post.threads_permalink or ''}",
            )
        # Fire and forget (notifier is async)
        import asyncio

        asyncio.create_task(await_notify)

    # ==================================================================
    # SCHEDULER-DRIVEN TICKS
    # ==================================================================

    async def tick_intake(self) -> None:
        """Process RECEIVED products (resume after restart)."""
        if self.paused:
            return
        for p in self.products.list_by_state("RECEIVED"):
            await self.intake(p.id)

    async def tick_ready_to_publish(self) -> None:
        """Retry READY_TO_PUBLISH products (gates may have opened)."""
        if self.paused:
            return
        for p in self.products.list_by_state("READY_TO_PUBLISH"):
            with self.session_factory() as db:
                winner = db.scalar(
                    select(ContentCandidate)
                    .where(ContentCandidate.product_id == p.id, ContentCandidate.selected.is_(True))
                    .order_by(ContentCandidate.id.desc())
                )
                opp = db.scalar(
                    select(Opportunity)
                    .where(Opportunity.product_id == p.id, Opportunity.status == "open")
                    .order_by(Opportunity.id.desc())
                )
                if winner is not None:
                    db.expunge(winner)
                if opp is not None:
                    db.expunge(opp)
            if winner is None:
                # No winner persisted (shouldn't happen); regenerate.
                self.products.set_state(p.id, "GENERATING", "recovery: regenerate")
                await self._generate_and_publish(p.id)
                continue
            try:
                post = await self.publisher.publish(
                    p, winner, winner.content, opportunity=opp
                )
                self._post_published(p.id, post)
            except PublishBlocked:
                continue  # still blocked; retry next tick
            except PublishFailed as e:
                await self._notify("error", f"⚠️ Threads publish failed: {str(e)[:150]}")
                continue

    async def tick_analytics(self) -> None:
        """Process due analytics checkpoints; complete products when done."""
        collected = await self.analytics.process_due()
        if collected:
            log.info("analytics: %d checkpoints collected", collected)
        # Complete products whose checkpoints are all done
        for p in self.products.list_by_state("ANALYTICS_PENDING"):
            with self.session_factory() as db:
                post = db.scalar(
                    select(PublishedPost)
                    .where(PublishedPost.product_id == p.id, PublishedPost.status == "published")
                    .order_by(PublishedPost.id.desc())
                )
                if post is None:
                    continue
                post_id = post.id
            if self.analytics.all_complete_for_post(post_id):
                self.products.set_state(p.id, "COMPLETED", "all checkpoints collected")
                summary = self.analytics.notify_24h(post_id)
                if summary:
                    await self._notify("info", summary)

    async def tick_failed_recovery(self) -> None:
        """Give FAILED products one automatic retry path (RECEIVED->RESOLVING etc.).

        Only re-attempts products that failed on transient stages and were
        recently submitted. Manual re-submission via Telegram always works too.
        """
        # Intentionally conservative: no auto-retry of FAILED to avoid loops.
        # (Re-submission is the supported recovery path.)
        pass
