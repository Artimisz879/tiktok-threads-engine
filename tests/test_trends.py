"""Trend normalization + velocity scoring tests."""
from datetime import timedelta

from app.trends.providers import extract_topics
from app.trends.scoring import (
    compute_velocity,
    cross_source_score,
    engagement_score,
    growth_score,
    malaysia_relevance_score,
    recency_score,
)
from app.trends.service import normalize_topic
from app.utils.timeutil import now_naive


class TestNormalize:
    def test_basic(self):
        assert normalize_topic("  Malaysia  HOT Weather!! ") == "malaysia hot weather"

    def test_unicode(self):
        assert normalize_topic("Café au lait") == "café au lait"


class TestExtractTopics:
    def test_repeated_phrase_becomes_topic(self):
        titles = [
            "Malaysia heatwave continues this week",
            "Malaysia heatwave expected to worsen",
            "Malaysia heatwave: MCM warns",
            "unrelated headline about football",
        ]
        hits = extract_topics(titles)
        topics = " | ".join(h.topic.lower() for h in hits)
        assert "malaysia heatwave" in topics

    def test_single_occurrence_ignored(self):
        titles = ["one unique headline", "another different story"]
        hits = extract_topics(titles)
        assert hits == []


class TestVelocity:
    def test_fresh_growing_cross_source_malay_is_high(self):
        now = now_naive()
        v = compute_velocity(
            last_seen=now - timedelta(hours=1),
            prev_volume=10,
            curr_volume=50,
            source_count=2,
            keywords=["malaysia", "panas", "cuaca"],
            topic="malaysia panas",
            now=now,
        )
        assert v > 0.7

    def test_old_stale_single_source_is_low(self):
        now = now_naive()
        v = compute_velocity(
            last_seen=now - timedelta(hours=72),
            prev_volume=50,
            curr_volume=5,
            source_count=1,
            keywords=["random"],
            topic="random",
            now=now,
        )
        assert v < 0.3

    def test_growth_doubling_is_full(self):
        assert growth_score(10, 20) == 1.0

    def test_growth_halving_is_zero(self):
        assert growth_score(20, 10) == 0.0

    def test_first_observation_neutral(self):
        assert growth_score(None, 5) == 0.3

    def test_recency_half_life(self):
        now = now_naive()
        assert recency_score(now, now) == 1.0
        assert abs(recency_score(now - timedelta(hours=6), now) - 0.5) < 0.01

    def test_engagement_log_scale(self):
        assert engagement_score(0) == 0.0
        assert engagement_score(99) < 1.0
        assert engagement_score(1000) == 1.0

    def test_cross_source(self):
        assert cross_source_score(1) == 0.5
        assert cross_source_score(2) == 1.0

    def test_malaysia_relevance(self):
        assert malaysia_relevance_score(["malaysia", "panas", "kuala lumpur"]) == 1.0
        assert malaysia_relevance_score(["quantum", "entangled", "photons"]) == 0.0
