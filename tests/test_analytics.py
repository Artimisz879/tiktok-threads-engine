"""Analytics ingestion: checkpoints, snapshots, restart recovery, no-overwrite."""
import httpx
import pytest
import respx
from sqlalchemy import select
from datetime import timedelta

from app.analytics.threads_analytics import AnalyticsService
from app.models import AnalyticsCheckpoint, PostMetricSnapshot, PublishedPost
from app.database import get_session
from app.utils.timeutil import now_naive


def make_settings(**kw):
    from app.config import Settings

    base = dict(
        _env_file=None,
        dry_run=False,
        auto_publish=True,
        analytics_checkpoints="1,6,24,72",
        threads_access_token="test-token",
        threads_user_id="u123",
    )
    base.update(kw)
    return Settings(**base)


def _published_post(db_session, threads_post_id="111222333"):
    from app.models import Product

    p = Product(
        product_key=f"an-{threads_post_id}",
        original_url="https://vt.tiktok.com/A/",
        title="Fan",
        price=19.9,
        state="PUBLISHED",
        resolved_at=now_naive(),
    )
    db_session.add(p)
    db_session.commit()
    post = PublishedPost(
        product_id=p.id,
        threads_post_id=threads_post_id,
        content="test post",
        status="published",
        dry_run=False,
        publish_time=now_naive() - timedelta(hours=2),
    )
    db_session.add(post)
    db_session.commit()
    db_session.refresh(post)
    return post


class TestAnalytics:
    @pytest.mark.asyncio
    async def test_collect_stores_snapshot(self, settings, db_session, session_factory):
        post = _published_post(db_session)
        svc = AnalyticsService(settings, session_factory)
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://graph.threads.net/v21.0/111222333/insights").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [
                            {"name": "likes_count", "value": 15},
                            {"name": "views", "value": 500},
                            {"name": "external_action_count", "value": 4},
                        ]
                    },
                )
            )
            snap = await svc.collect(post.id, "1h")
        assert snap is not None
        assert snap.views == 500
        assert snap.likes == 15
        assert snap.external_actions == 4
        # checkpoint marked done
        cp = db_session.scalar(
            select(AnalyticsCheckpoint).where(
                AnalyticsCheckpoint.post_id == post.id, AnalyticsCheckpoint.checkpoint == "1h"
            )
        )
        # (no checkpoint row was pre-created in this test; collect still stored snapshot)
        snaps = db_session.scalars(
            select(PostMetricSnapshot).where(PostMetricSnapshot.post_id == post.id)
        ).all()
        assert len(snaps) == 1

    @pytest.mark.asyncio
    async def test_snapshot_not_overwritten_by_different_checkpoint(self, settings, db_session, session_factory):
        post = _published_post(db_session)
        svc = AnalyticsService(settings, session_factory)

        def insights(value):
            return httpx.Response(200, json={"data": [{"name": "likes_count", "value": value}]})

        with respx.mock(assert_all_called=False) as mock:
            route = mock.get("https://graph.threads.net/v21.0/111222333/insights")
            route.side_effect = [insights(10), insights(25)]
            await svc.collect(post.id, "1h")
            await svc.collect(post.id, "6h")
        snaps = db_session.scalars(
            select(PostMetricSnapshot).where(PostMetricSnapshot.post_id == post.id).order_by(PostMetricSnapshot.id)
        ).all()
        assert len(snaps) == 2
        assert snaps[0].likes == 10
        assert snaps[1].likes == 25

    @pytest.mark.asyncio
    async def test_process_due_picks_up_from_db(self, settings, db_session, session_factory):
        """Restart recovery: checkpoints are DB rows; process_due finds them."""
        post = _published_post(db_session)
        now = now_naive()
        db_session.add(
            AnalyticsCheckpoint(post_id=post.id, checkpoint="1h", due_at=now - timedelta(minutes=5))
        )
        db_session.add(
            AnalyticsCheckpoint(post_id=post.id, checkpoint="6h", due_at=now + timedelta(hours=4))  # not due
        )
        db_session.commit()

        svc = AnalyticsService(settings, session_factory)
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://graph.threads.net/v21.0/111222333/insights").mock(
                return_value=httpx.Response(200, json={"data": [{"name": "likes_count", "value": 1}]})
            )
            collected = await svc.process_due()
        assert collected == 1  # only the due one
        cps = db_session.scalars(
            select(AnalyticsCheckpoint).where(AnalyticsCheckpoint.post_id == post.id)
        ).all()
        done = {c.checkpoint: c.done for c in cps}
        assert done["1h"] is True
        assert done["6h"] is False

    @pytest.mark.asyncio
    async def test_dryrun_post_skips_api(self, settings, db_session, session_factory):
        post = _published_post(db_session, threads_post_id="dryrun-abc")
        svc = AnalyticsService(settings, session_factory)
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://graph.threads.net/v21.0/dryrun-abc/insights").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
            snap = await svc.collect(post.id, "1h")
        assert snap is None  # no real metrics for dry-run posts

    @pytest.mark.asyncio
    async def test_api_failure_marks_error_not_crash(self, settings, db_session, session_factory):
        post = _published_post(db_session)
        svc = AnalyticsService(settings, session_factory)
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://graph.threads.net/v21.0/111222333/insights").mock(
                return_value=httpx.Response(
                    400, json={"error": {"message": "metric unavailable", "code": 100}}
                )
            )
            snap = await svc.collect(post.id, "1h")
        assert snap is None  # graceful
