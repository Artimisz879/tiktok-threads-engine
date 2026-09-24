"""End-to-end pipeline test with mock LLM: link in -> dry-run post out.

Also covers scheduler recovery semantics:
- a product stuck mid-pipeline resumes correctly
- analytics checkpoints survive 'restart' (new service instances, same DB)

NOTE: all DB access in tests goes through fresh sessions (get_session),
because service objects own their own short-lived sessions.
"""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.database import get_session
from app.models import ContentCandidate, Product, PublishedPost, Trend
from app.utils.timeutil import now_naive


def mutate_product(pid: int, **fields):
    with get_session() as db:
        p = db.get(Product, pid)
        for k, v in fields.items():
            setattr(p, k, v)
        db.commit()


def get_product_state(pid: int) -> dict:
    with get_session() as db:
        p = db.get(Product, pid)
        return {"state": p.state, "state_reason": p.state_reason, "analysis": p.analysis}


async def seed_hot_trend():
    with get_session() as db:
        db.add(
            Trend(
                topic="Malaysia hot weather",
                keywords=["panas", "cuaca panas", "heat"],
                sources=["news_rss"],
                discussion_volume=42,
                velocity=0.82,
                sentiment="negative",
                sensitivity="neutral",
                confidence=0.8,
                active=True,
                last_seen=now_naive(),
            )
        )
        db.commit()


async def wait_for_state(pid: int, states, tries=30, delay=0.1):
    for _ in range(tries):
        await asyncio.sleep(delay)
        st = get_product_state(pid)["state"]
        if st in states:
            return st
    return get_product_state(pid)["state"]


@pytest.mark.asyncio
async def test_full_pipeline_smart_mode_dry_run(pipeline, settings):
    """RECEIVED -> ... -> PUBLISHED (dry run) with a matching hot-weather trend."""
    pipe, notes = pipeline
    await seed_hot_trend()

    from app.product.service import ProductService

    svc = ProductService(settings, get_session)
    p, _ = svc.submit("https://vt.tiktok.com/E2E1/", mode="smart", chat_id=12345)
    pid = p.id

    # Set resolved metadata (resolver network is mocked out here).
    mutate_product(
        pid,
        title="Portable Turbo Fan USB Rechargeable",
        price=19.9,
        description="Small USB fan, 3 speeds, clip on.",
        confidence=0.9,
        resolved_url="https://www.tiktok.com/shop/e2e1",
        resolved_at=now_naive(),
    )

    # Run intake (analyze).
    await pipe.intake(pid)
    st = get_product_state(pid)
    assert st["state"] == "WAITING_FOR_OPPORTUNITY", st
    assert st["analysis"] is not None

    # Opportunity scan should find the hot-weather trend and advance to generation.
    await pipe.scan_opportunities()
    st = await wait_for_state(pid, ("PUBLISHED", "COMPLETED", "FAILED", "SKIPPED", "READY_TO_PUBLISH"))
    assert st in ("PUBLISHED", "COMPLETED"), f"unexpected state {st}: {get_product_state(pid)['state_reason']}"

    with get_session() as db:
        post = db.scalar(select(PublishedPost).where(PublishedPost.product_id == pid))
        assert post is not None
        assert post.dry_run is True
        assert "affiliate link" in post.content

        cands = db.scalars(select(ContentCandidate).where(ContentCandidate.product_id == pid)).all()
        assert len(cands) == 5
        assert sum(1 for c in cands if c.selected) == 1

        from app.models import Opportunity

        opp = db.scalar(select(Opportunity).where(Opportunity.product_id == pid))
        assert opp is not None
        assert opp.kind == "trend"
        assert opp.score >= settings.opportunity_threshold


@pytest.mark.asyncio
async def test_instant_mode_skips_queue(pipeline, settings):
    pipe, notes = pipeline
    from app.product.service import ProductService

    svc = ProductService(settings, get_session)
    p, _ = svc.submit("https://vt.tiktok.com/E2E2/", mode="instant", chat_id=12345)
    pid = p.id
    mutate_product(
        pid,
        title="Portable Turbo Fan USB Rechargeable",
        price=19.9,
        description="Small USB fan.",
        confidence=0.9,
        resolved_at=now_naive(),
    )

    await pipe.intake(pid)
    st = await wait_for_state(pid, ("PUBLISHED", "COMPLETED", "FAILED", "SKIPPED"))
    assert st in ("PUBLISHED", "COMPLETED"), f"unexpected {st}: {get_product_state(pid)['state_reason']}"
    # Instant mode must never have waited in the opportunity queue.
    assert "waiting" not in get_product_state(pid)["state_reason"].lower()


@pytest.mark.asyncio
async def test_restart_resumes_stuck_product(pipeline, settings):
    """A product left in RESOLVING (crash mid-intake) resumes on next intake."""
    pipe, notes = pipeline
    with get_session() as db:
        p = Product(
            product_key="resume-1",
            original_url="https://vt.tiktok.com/RES1/",
            title="Portable Turbo Fan",
            price=19.9,
            description="USB fan",
            confidence=0.9,
            state="RESOLVING",  # stuck
            mode="smart",
            resolved_at=now_naive(),
        )
        db.add(p)
        db.commit()
        pid = p.id

    await pipe.intake(pid)  # should not raise, should continue
    st = get_product_state(pid)["state"]
    assert st in ("WAITING_FOR_OPPORTUNITY", "ANALYZED", "GENERATING"), st


@pytest.mark.asyncio
async def test_analytics_checkpoints_survive_restart(pipeline, settings):
    """Checkpoints are DB rows: a 'new' AnalyticsService sees them after restart."""
    from app.analytics.threads_analytics import AnalyticsService
    from app.models import AnalyticsCheckpoint

    with get_session() as db:
        p = Product(
            product_key="restart-an",
            original_url="https://vt.tiktok.com/RA/",
            title="Fan",
            price=19.9,
            state="ANALYTICS_PENDING",
            resolved_at=now_naive(),
        )
        db.add(p)
        db.commit()
        post = PublishedPost(
            product_id=p.id,
            threads_post_id="555666777",
            content="post",
            status="published",
            dry_run=False,
            publish_time=now_naive() - timedelta(hours=2),
        )
        db.add(post)
        db.commit()
        db.add(
            AnalyticsCheckpoint(post_id=post.id, checkpoint="1h", due_at=now_naive() - timedelta(minutes=1))
        )
        db.commit()
        post_id = post.id

    # Simulate restart: brand-new service instance on the same DB.
    svc = AnalyticsService(settings, get_session)
    assert not svc.all_complete_for_post(post_id)
    with get_session() as db:
        due = db.scalars(
            select(AnalyticsCheckpoint).where(
                AnalyticsCheckpoint.done.is_(False), AnalyticsCheckpoint.due_at <= now_naive()
            )
        ).all()
        assert len(due) == 1
        assert due[0].checkpoint == "1h"
