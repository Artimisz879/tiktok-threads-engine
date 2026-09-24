"""Shared fixtures: temp DB, mock-LLM settings, service instances."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

# Make project root importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def tmp_db(tmp_path):
    from app.database import init_db, dispose_engine

    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    yield db_path
    dispose_engine()


@pytest.fixture()
def settings(tmp_db):
    from app.config import Settings

    return Settings(
        _env_file=None,
        llm_provider="mock",
        dry_run=True,
        auto_publish=True,
        db_path=tmp_db,
        data_dir=os.path.dirname(tmp_db),
        quiet_hours="00:00-00:00",  # never quiet in tests
        max_posts_per_day=10,
        min_hours_between_posts=0,
        opportunity_threshold=50,
        telegram_allowed_user_id=12345,
        telegram_bot_token="test-token",
    )


@pytest.fixture()
def db_session(tmp_db):
    from app.database import get_session

    return get_session()


@pytest.fixture()
def session_factory(tmp_db):
    from app.database import get_session

    return get_session


@pytest.fixture()
def pipeline(settings):
    from app.pipeline import Pipeline

    notes = []

    async def notifier(level, text):
        notes.append((level, text))

    return Pipeline(settings, notifier=notifier), notes


@pytest.fixture()
def product_row(db_session):
    """A product in ANALYZED state with mock intelligence, ready for the pipeline."""
    from app.models import Product
    from app.utils.timeutil import now_naive

    p = Product(
        product_key="test-key-1",
        original_url="https://vt.tiktok.com/TEST1/",
        resolved_url="https://www.tiktok.com/shop/TEST1",
        title="Portable Turbo Fan USB Rechargeable",
        price=19.9,
        description="Small USB rechargeable fan, 3 speeds, clip-on or handheld.",
        confidence=0.9,
        state="RESOLVING",
        mode="smart",
        resolved_at=now_naive(),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p
