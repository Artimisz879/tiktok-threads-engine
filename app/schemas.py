"""Pydantic schemas: LLM structured outputs + API response models.

Every LLM response is validated against these. Malformed output is retried,
never blindly trusted.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ---------------- Product resolution ----------------

class ResolvedProduct(BaseModel):
    original_url: str
    resolved_url: str | None = None
    product_id: str | None = None
    title: str = ""
    price: float | None = None
    currency: str = "MYR"
    description: str = ""
    images: list[str] = Field(default_factory=list)
    category: str = ""
    seller: str = ""
    features: list[str] = Field(default_factory=list)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    source: str = ""  # which provider produced this


# ---------------- Product intelligence (LLM call 1) ----------------

class ProductIntelligence(BaseModel):
    product_summary: str = Field(min_length=10)
    target_audiences: list[str] = Field(min_length=1)
    problems_solved: list[str] = Field(min_length=1)
    pain_points: list[str] = Field(min_length=1)
    emotional_triggers: list[str] = Field(default_factory=list)
    purchase_motivations: list[str] = Field(default_factory=list)
    content_opportunities: list[str] = Field(default_factory=list)
    risky_claims_to_avoid: list[str] = Field(default_factory=list)
    recommended_tone: list[str] = Field(default_factory=list)
    category: str = ""
    price_range: Literal["under_rm50", "rm50_200", "rm200_500", "over_rm500", "unknown"] = "unknown"

    @field_validator("target_audiences", "pain_points", "problems_solved")
    @classmethod
    def _clean_lists(cls, v: list[str]) -> list[str]:
        return [x.strip() for x in v if x and x.strip()][:12]


# ---------------- Trend relevance (LLM call 2, batched) ----------------

class TrendRelevance(BaseModel):
    trend_id: int
    relevant: bool
    relevance_score: float = Field(0.0, ge=0.0, le=1.0)
    reason: str = ""
    angle_hint: str = ""  # how a natural connection could look (empty if not relevant)


class TrendRelevanceBatch(BaseModel):
    results: list[TrendRelevance] = Field(default_factory=list)


# ---------------- Content candidates (LLM call 3) ----------------

class ContentCandidateOut(BaseModel):
    angle: str = Field(min_length=3, max_length=80)
    hook_type: Literal[
        "story", "observation", "problem_solution", "humour", "tip",
        "question", "comparison", "curiosity", "trend",
    ] = "observation"
    content: str = Field(min_length=40, max_length=1500)
    why_it_works: str = ""


class CandidateBatch(BaseModel):
    candidates: list[ContentCandidateOut] = Field(min_length=1, max_length=8)


# ---------------- Critic (LLM call 4) ----------------

class CriticVerdict(BaseModel):
    candidate_index: int
    human_sounding: float = Field(0.0, ge=0.0, le=1.0)
    humour: float = Field(0.0, ge=0.0, le=1.0)
    curiosity: float = Field(0.0, ge=0.0, le=1.0)
    relatability: float = Field(0.0, ge=0.0, le=1.0)
    product_relevance: float = Field(0.0, ge=0.0, le=1.0)
    audience_relevance: float = Field(0.0, ge=0.0, le=1.0)
    originality: float = Field(0.0, ge=0.0, le=1.0)
    usefulness: float = Field(0.0, ge=0.0, le=1.0)
    purchase_intent: float = Field(0.0, ge=0.0, le=1.0)
    trend_relevance: float = Field(0.0, ge=0.0, le=1.0)
    ai_sounding_penalty: float = Field(0.0, ge=0.0, le=1.0)
    overly_salesy_penalty: float = Field(0.0, ge=0.0, le=1.0)
    misinformation_risk: float = Field(0.0, ge=0.0, le=1.0)
    reputational_risk: float = Field(0.0, ge=0.0, le=1.0)
    policy_risk: float = Field(0.0, ge=0.0, le=1.0)
    safe: bool = True
    rejection_reason: str = ""
    improved_content: str | None = None  # optional rewrite of the winner
    overall: float = Field(0.0, ge=0.0, le=1.0)


class CriticReport(BaseModel):
    verdicts: list[CriticVerdict] = Field(default_factory=list)
    winner_index: int | None = None
    publish: bool = False
    notes: str = ""


# ---------------- Analytics ----------------

class MetricSnapshot(BaseModel):
    checkpoint: str
    views: int | None = None
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    shares: int | None = None
    external_actions: int | None = None
    raw: dict = Field(default_factory=dict)
    captured_at: datetime
