"""Opportunity scoring: never force a trend; threshold gates; sensitivity rejects."""
from datetime import timedelta

from app.opportunity.engine import best_opportunity, evergreen_opportunity, score_opportunity
from app.models import Trend
from app.utils.timeutil import now_naive


def make_trend(velocity=0.8, confidence=0.8, sensitivity="neutral", last_seen=None):
    return Trend(
        topic="test",
        velocity=velocity,
        confidence=confidence,
        sensitivity=sensitivity,
        last_seen=last_seen or now_naive(),
    )


class TestScore:
    def test_strong_match_high(self):
        score, bd = score_opportunity(
            velocity=0.9, relevance=0.9, trend_age_hours=2, confidence=0.9,
        )
        assert score > 60
        assert bd["score"] == score

    def test_weak_match_low(self):
        score, _ = score_opportunity(
            velocity=0.3, relevance=0.2, trend_age_hours=30, confidence=0.5,
        )
        assert score < 25

    def test_sensitive_trend_always_zero(self):
        score, bd = score_opportunity(
            velocity=0.95, relevance=0.95, trend_age_hours=1, confidence=0.9,
            sensitivity="sensitive:death",
        )
        assert score == 0.0
        assert bd["reject"] == "sensitive trend"

    def test_stale_trend_decays(self):
        fresh, _ = score_opportunity(velocity=0.8, relevance=0.8, trend_age_hours=1, confidence=0.8)
        stale, _ = score_opportunity(velocity=0.8, relevance=0.8, trend_age_hours=60, confidence=0.8)
        assert fresh > stale


class TestBestOpportunity:
    def test_returns_best_above_threshold(self):
        now = now_naive()
        t1 = make_trend(velocity=0.9, last_seen=now)
        t2 = make_trend(velocity=0.5, last_seen=now)
        matches = [
            {"trend": t2, "relevance_score": 0.3, "reason": "weak", "angle_hint": ""},
            {"trend": t1, "relevance_score": 0.9, "reason": "strong", "angle_hint": "heat fix"},
        ]
        best = best_opportunity(matches, threshold=50, now=now)
        assert best is not None
        assert best["trend"] is t1
        assert best["score"] >= 50

    def test_none_above_threshold_returns_none(self):
        now = now_naive()
        t = make_trend(velocity=0.3, last_seen=now)
        matches = [{"trend": t, "relevance_score": 0.2, "reason": "no", "angle_hint": ""}]
        best = best_opportunity(matches, threshold=75, now=now)
        assert best is None  # -> evergreen fallback upstream

    def test_sensitive_never_selected_even_if_relevant(self):
        now = now_naive()
        t = make_trend(velocity=0.95, sensitivity="sensitive:disaster", last_seen=now)
        matches = [{"trend": t, "relevance_score": 0.95, "reason": "tragic event", "angle_hint": ""}]
        best = best_opportunity(matches, threshold=10, now=now)
        assert best is None

    def test_evergreen_fallback_shape(self):
        opp = evergreen_opportunity()
        assert opp["kind"] == "evergreen"
        assert opp["trend"] is None
        assert opp["topic"] is None
