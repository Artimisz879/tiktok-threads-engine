"""Threads client (mocked) + publisher gates + dry-run + duplicate publish prevention."""
import httpx
import pytest
import respx

from app.models import ContentCandidate, Product, PublishedPost
from app.publishing.publisher import PublishBlocked, Publisher
from app.publishing.threads_client import ThreadsAPIError, ThreadsClient
from app.database import get_session
from app.utils.timeutil import now_naive


def make_settings(**kw):
    from app.config import Settings

    base = dict(
        _env_file=None,
        dry_run=True,
        auto_publish=True,
        quiet_hours="00:00-00:00",
        max_posts_per_day=2,
        min_hours_between_posts=0,
        similarity_threshold=0.62,
        affiliate_disclosure_enabled=True,
        affiliate_disclosure_text="affiliate link",
        threads_access_token="test-token",
        threads_user_id="u123",
        threads_api_version="v21.0",
    )
    base.update(kw)
    return Settings(**base)


class TestThreadsClient:
    @pytest.mark.asyncio
    async def test_publish_text(self):
        s = make_settings()
        client = ThreadsClient(s)
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://graph.threads.net/v21.0/me/threads").mock(
                return_value=httpx.Response(200, json={"id": "1234567890"})
            )
            result = await client.publish_text("hello world", link="https://vt.tiktok.com/x")
        assert result["id"] == "1234567890"
        assert "1234567890" in result["permalink"]

    @pytest.mark.asyncio
    async def test_publish_rate_limited(self):
        s = make_settings()
        client = ThreadsClient(s)
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://graph.threads.net/v21.0/me/threads").mock(
                return_value=httpx.Response(
                    429,
                    headers={"retry-after": "120"},
                    json={"error": {"message": "rate limit", "code": 4}},
                )
            )
            with pytest.raises(ThreadsAPIError) as ei:
                await client.publish_text("hi")
        assert ei.value.is_rate_limit
        assert ei.value.retry_after == 120.0

    @pytest.mark.asyncio
    async def test_auth_error_detected(self):
        s = make_settings()
        client = ThreadsClient(s)
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://graph.threads.net/v21.0/me").mock(
                return_value=httpx.Response(
                    400,
                    json={"error": {"message": "Invalid OAuth 2.0 Access Token", "code": 190}},
                )
            )
            with pytest.raises(ThreadsAPIError) as ei:
                await client.me()
        assert ei.value.is_auth_error

    @pytest.mark.asyncio
    async def test_insights_parsing(self):
        from app.analytics.threads_analytics import parse_insights

        raw = {
            "data": [
                {"name": "likes_count", "value": 42},
                {"name": "replies_count", "value": 7},
                {"name": "external_action_count", "value": 3},
                {"name": "views", "value": 1200},
            ]
        }
        out = parse_insights(raw)
        assert out["likes"] == 42
        assert out["replies"] == 7
        assert out["external_actions"] == 3
        assert out["views"] == 1200

    def test_oauth_url_shape(self):
        s = make_settings(threads_app_id="999", threads_redirect_uri="http://localhost:8321/threads/callback")
        client = ThreadsClient(s)
        url = client.oauth_authorize_url("abc")
        assert "client_id=999" in url
        assert "threads_content_publish" in url
        assert "state=abc" in url


def _make_product_and_candidate(db_session, content="malaysia panas gila today and this little fan saved my commute (affiliate link)", key=None):
    p = Product(
        product_key=key or f"pub-{content[:8]}",
        original_url="https://vt.tiktok.com/PUB/",
        title="Portable Turbo Fan",
        price=19.9,
        state="READY_TO_PUBLISH",
        resolved_at=now_naive(),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    c = ContentCandidate(product_id=p.id, angle="humour", hook_type="humour", content=content)
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return p, c


class TestPublisher:
    @pytest.mark.asyncio
    async def test_dry_run_publishes_nothing(self, settings, db_session, session_factory):
        p, c = _make_product_and_candidate(db_session)
        pub = Publisher(settings, session_factory)
        post = await pub.publish(p, c, c.content)
        assert post.dry_run is True
        assert post.threads_post_id.startswith("dryrun-")
        assert post.threads_permalink is None
        # disclosure appended
        assert "affiliate link" in post.content
        # state advanced
        db_session.refresh(p)
        assert p.state == "PUBLISHED"
        # checkpoints scheduled
        from app.models import AnalyticsCheckpoint
        from sqlalchemy import select

        cps = db_session.scalars(select(AnalyticsCheckpoint).where(AnalyticsCheckpoint.post_id == post.id)).all()
        assert len(cps) == 4  # 1,6,24,72h

    @pytest.mark.asyncio
    async def test_duplicate_publish_prevented(self, settings, db_session, session_factory):
        content = "first post about the fan and the heat today (affiliate link)"
        p, c = _make_product_and_candidate(db_session, content=content)
        pub = Publisher(settings, session_factory)
        post1 = await pub.publish(p, c, c.content)
        # Simulate a second publish attempt for the same product (restart bug
        # scenario). Content is different enough to pass the similarity gate,
        # so we reach the idempotency check: product already has a post.
        db_session.refresh(p)
        p.state = "READY_TO_PUBLISH"  # force back
        db_session.commit()
        other_content = "today the rain came so fast that everyone in the office forgot their umbrellas again"
        c2 = ContentCandidate(product_id=p.id, angle="story", hook_type="story", content=other_content)
        db_session.add(c2)
        db_session.commit()
        db_session.refresh(c2)
        post2 = await pub.publish(p, c2, other_content)
        assert post2.id == post1.id  # same row returned, no duplicate
        from sqlalchemy import select
        from app.models import PublishedPost

        count = db_session.scalar(
            select(PublishedPost).where(PublishedPost.product_id == p.id).limit(2)
        )
        posts = db_session.scalars(select(PublishedPost).where(PublishedPost.product_id == p.id)).all()
        assert len(posts) == 1

    @pytest.mark.asyncio
    async def test_similarity_gate_blocks(self, settings, db_session, session_factory):
        # dry_run stays True: the similarity gate ALWAYS blocks, even in dry run.
        content = "the heat in malaysia is unbearable and this portable fan in my bag is my best friend right now (affiliate link)"
        p, c = _make_product_and_candidate(db_session, content=content)
        pub = Publisher(settings, session_factory)
        await pub.publish(p, c, c.content)
        # Now a nearly-identical post for a different product must be blocked.
        p2, c2 = _make_product_and_candidate(
            db_session,
            content="the heat in malaysia is unbearable and this portable fan in my bag is my best friend right now (affiliate link)",
            key="pub-other-product",
        )
        db_session.commit()
        with pytest.raises(PublishBlocked):
            await pub.publish(p2, c2, c2.content)

    def test_quiet_hours_gate(self, settings, db_session, session_factory):
        settings.dry_run = False
        settings.quiet_hours = "00:00-23:59"  # always quiet
        p, c = _make_product_and_candidate(db_session)
        pub = Publisher(settings, session_factory)
        with pytest.raises(PublishBlocked):
            pub.check_gates(c.content)

    def test_max_posts_per_day_gate(self, settings, db_session, session_factory):
        settings.dry_run = False
        settings.max_posts_per_day = 0
        p, c = _make_product_and_candidate(db_session)
        pub = Publisher(settings, session_factory)
        with pytest.raises(PublishBlocked):
            pub.check_gates(c.content)
