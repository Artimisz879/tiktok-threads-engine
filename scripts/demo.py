"""End-to-end dry-run demo: no credentials needed.

Submits a fake product URL, runs the whole pipeline with the mock LLM,
and shows what would be published. Useful for verifying the system works
before wiring real credentials.

Usage:
    python -m scripts.demo
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("AUTO_PUBLISH", "true")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")


async def run() -> None:
    from app.config import Settings
    from app.database import init_db, dispose_engine
    from app.pipeline import Pipeline
    from app.product.service import ProductService
    from app.trends.service import TrendService

    tmp = tempfile.mkdtemp(prefix="tte-demo-")
    db_path = os.path.join(tmp, "demo.db")

    s = Settings(
        _env_file=None,
        llm_provider="mock",
        dry_run=True,
        auto_publish=True,
        db_path=db_path,
        data_dir=tmp,
        quiet_hours="00:00-00:00",  # disable quiet hours for the demo
        max_posts_per_day=10,
        min_hours_between_posts=0,
        opportunity_threshold=50,
    )
    init_db(db_path)

    notifications: list[str] = []

    async def notifier(level: str, text: str) -> None:
        notifications.append(text)
        print(f"\n--- TELEGRAM ({level}) ---\n{text}\n")

    pipeline = Pipeline(s, notifier=notifier)

    # Seed a trend so the opportunity engine has something to match.
    trends = TrendService(s)
    from app.models import Trend
    from app.utils.timeutil import now_naive
    from app.database import get_session

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

    # Submit a product (use a tiktok.com URL shape; the mock resolver path
    # is exercised via direct state manipulation to avoid network in the demo).
    products = ProductService(s)
    url = "https://vt.tiktok.com/DEMO123/"
    product, is_new = products.submit(url, mode="smart", chat_id=1)
    print(f"submitted product id={product.id} (new={is_new})")

    # Bypass the network resolver for the demo: set metadata directly and put
    # the product in the state the resolver would leave it in (ANALYZED).
    from app.models import Product
    from app.utils.timeutil import now_naive

    with get_session() as db:
        p = db.get(Product, product.id)
        p.resolved_url = url
        p.title = "Portable Turbo Fan USB Rechargeable"
        p.price = 19.9
        p.description = "Small USB rechargeable fan, 3 speeds, clip-on or handheld."
        p.confidence = 0.9
        p.resolved_at = now_naive()
        p.state = "ANALYZED"
        db.commit()

    # Run the pipeline stages.
    await pipeline._analyze(product.id)
    await pipeline.scan_opportunities()

    # Give the generated task a moment to run (it's a create_task).
    await asyncio.sleep(1.0)

    # Print final state.
    with get_session() as db:
        p = db.get(Product, product.id)
        print("\n" + "=" * 60)
        print(f"FINAL STATE: {p.state}")
        print(f"REASON: {p.state_reason}")
        from sqlalchemy import select
        from app.models import PublishedPost, ContentCandidate

        post = db.scalar(select(PublishedPost).order_by(PublishedPost.id.desc()))
        if post is not None:
            print("\n" + "=" * 60)
            print("WOULD PUBLISH (dry run):")
            print("-" * 60)
            print(post.content)
            print("-" * 60)
            print(f"post id: {post.threads_post_id}")
            print(f"dry_run: {post.dry_run}")
        cands = db.scalars(select(ContentCandidate)).all()
        print(f"\ncandidates generated: {len(cands)}")
        for c in cands:
            mark = "✅ SELECTED" if c.selected else "   "
            print(f"  {mark} [{c.hook_type}] {c.angle}: {c.content[:70]}…")

    dispose_engine()
    print("\nDemo complete.")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
