"""All ORM models in one module (SQLite V1; Postgres-compatible types)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils.timeutil import now_utc


def _utcnow() -> datetime:
    # Naive UTC: SQLite stores naive datetimes; all comparisons use now_naive().
    return now_utc().replace(tzinfo=None)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Deterministic dedupe key: resolved product URL (or submitted URL if unresolvable)
    product_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    original_url: Mapped[str] = mapped_column(String(1024))
    resolved_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="MYR")
    description: Mapped[str] = mapped_column(Text, default="")
    images: Mapped[list] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(128), default="")
    seller: Mapped[str] = mapped_column(String(256), default="")
    features: Mapped[list] = mapped_column(JSON, default=list)
    # Metadata resolution confidence 0..1
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # LLM intelligence (JSON blob of ProductIntelligence)
    analysis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # State machine state
    state: Mapped[str] = mapped_column(String(32), default="RECEIVED", index=True)
    state_reason: Mapped[str] = mapped_column(String(512), default="")
    mode: Mapped[str] = mapped_column(String(16), default="smart")  # smart | instant
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    opportunities: Mapped[list["Opportunity"]] = relationship(back_populates="product")
    posts: Mapped[list["PublishedPost"]] = relationship(back_populates="product")


class Trend(Base):
    __tablename__ = "trends"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    discussion_volume: Mapped[int] = mapped_column(Integer, default=0)
    velocity: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    sentiment: Mapped[str] = mapped_column(String(32), default="")
    sensitivity: Mapped[str] = mapped_column(String(32), default="neutral")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    snapshots: Mapped[list["TrendSnapshot"]] = relationship(back_populates="trend")


class TrendSnapshot(Base):
    __tablename__ = "trend_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    trend_id: Mapped[int] = mapped_column(ForeignKey("trends.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    discussion_volume: Mapped[int] = mapped_column(Integer, default=0)
    velocity: Mapped[float] = mapped_column(Float, default=0.0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)

    trend: Mapped[Trend] = relationship(back_populates="snapshots")


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    trend_id: Mapped[int | None] = mapped_column(
        ForeignKey("trends.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), default="trend")  # trend | evergreen
    score: Mapped[float] = mapped_column(Float, default=0.0)  # 0..100
    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="open")  # open | used | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    product: Mapped[Product] = relationship(back_populates="opportunities")
    trend: Mapped[Trend | None] = relationship()


class ContentCandidate(Base):
    __tablename__ = "content_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"), nullable=True)
    angle: Mapped[str] = mapped_column(String(128), default="")
    hook_type: Mapped[str] = mapped_column(String(64), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    critic: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # scores + verdict
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    rejected_reason: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PublishedPost(Base):
    __tablename__ = "published_posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("content_candidates.id"), nullable=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"), nullable=True)
    threads_post_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True, index=True)
    threads_permalink: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    disclosure: Mapped[str] = mapped_column(String(256), default="")
    trend_id: Mapped[int | None] = mapped_column(ForeignKey("trends.id"), nullable=True)
    strategy: Mapped[str] = mapped_column(String(64), default="")  # angle label
    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="scheduled")  # scheduled|published|failed
    error: Mapped[str] = mapped_column(String(1024), default="")

    product: Mapped[Product] = relationship(back_populates="posts")
    trend: Mapped[Trend | None] = relationship()
    snapshots: Mapped[list["PostMetricSnapshot"]] = relationship(back_populates="post")
    checkpoints: Mapped[list["AnalyticsCheckpoint"]] = relationship(back_populates="post")


class PostMetricSnapshot(Base):
    __tablename__ = "post_metric_snapshots"
    __table_args__ = (UniqueConstraint("post_id", "checkpoint", name="uq_post_checkpoint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("published_posts.id"), index=True)
    checkpoint: Mapped[str] = mapped_column(String(16))  # "1h","6h","24h","72h"
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replies: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reposts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quotes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_actions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    post: Mapped[PublishedPost] = relationship(back_populates="snapshots")


class AnalyticsCheckpoint(Base):
    """Pending analytics job (persisted so restarts don't lose work)."""
    __tablename__ = "analytics_checkpoints"
    __table_args__ = (UniqueConstraint("post_id", "checkpoint", name="uq_checkpoint_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("published_posts.id"), index=True)
    checkpoint: Mapped[str] = mapped_column(String(16))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(String(512), default="")

    post: Mapped[PublishedPost] = relationship(back_populates="checkpoints")


class AffiliateConversion(Base):
    __tablename__ = "affiliate_conversions"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("published_posts.id"), nullable=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="none")
    orders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_commission: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="MYR")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class StrategyMemory(Base):
    """Aggregated performance learnings, refreshed daily. SQL retrieval, no ML."""
    __tablename__ = "strategy_memory"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # category|angle|hook|time_window|trend_combo
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    posts_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(16), default="info")  # info|warn|error
    category: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(String(1024), default="")
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class TelegramJob(Base):
    """Products submitted via Telegram + notification bookkeeping."""
    __tablename__ = "telegram_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="smart")
    last_notified_state: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
