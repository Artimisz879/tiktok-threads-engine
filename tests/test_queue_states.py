"""Queue state machine + duplicate prevention + restart idempotency."""
import pytest

from app.models import Product
from app.product.service import ProductService, product_key_for
from app.security.url_validation import URLValidationError
from app.database import get_session
from app.state_machine import StateError, can_transition, is_terminal, transition


class TestStateMachine:
    def test_happy_path(self):
        path = [
            "RECEIVED", "RESOLVING", "ANALYZED", "QUEUED", "WAITING_FOR_OPPORTUNITY",
            "GENERATING", "REVIEWING", "READY_TO_PUBLISH", "PUBLISHING", "PUBLISHED",
            "ANALYTICS_PENDING", "COMPLETED",
        ]
        for a, b in zip(path, path[1:]):
            assert can_transition(a, b), f"{a} -> {b} should be allowed"

    def test_instant_mode_skip(self):
        assert can_transition("ANALYZED", "GENERATING")
        assert can_transition("QUEUED", "GENERATING")

    def test_illegal_transitions_rejected(self):
        assert not can_transition("COMPLETED", "PUBLISHING")
        assert not can_transition("RECEIVED", "PUBLISHED")
        assert not can_transition("PUBLISHED", "RECEIVED")
        with pytest.raises(StateError):
            transition("COMPLETED", "PUBLISHING")

    def test_terminal_states(self):
        assert is_terminal("COMPLETED")
        assert is_terminal("FAILED")
        assert is_terminal("SKIPPED")
        assert not is_terminal("PUBLISHED")

    def test_failed_can_recover(self):
        assert can_transition("FAILED", "RESOLVING")
        assert can_transition("FAILED", "GENERATING")


class TestDuplicatePrevention:
    def test_same_url_same_key(self):
        assert product_key_for("https://vt.tiktok.com/ABC/") == product_key_for("https://vt.tiktok.com/ABC")
        assert product_key_for("https://vt.tiktok.com/ABC/") != product_key_for("https://vt.tiktok.com/DEF/")

    def test_resubmit_returns_existing(self, settings, db_session):
        svc = ProductService(settings, get_session)
        url = "https://vt.tiktok.com/DUP1/"
        p1, new1 = svc.submit(url, mode="smart")
        p2, new2 = svc.submit(url, mode="smart")
        assert new1 is True
        assert new2 is False
        assert p1.id == p2.id

    def test_disallowed_url_rejected(self, settings, db_session):
        svc = ProductService(settings, get_session)
        with pytest.raises(URLValidationError):
            svc.submit("https://evil.example.com/product", mode="smart")

    def test_failed_product_can_be_resubmitted(self, settings, db_session):
        svc = ProductService(settings, get_session)
        url = "https://vt.tiktok.com/RETRY1/"
        p, _ = svc.submit(url, mode="smart")
        # Force it to FAILED (fresh session — the service's session is closed).
        with get_session() as db:
            row = db.get(Product, p.id)
            row.state = "FAILED"
            db.commit()
        p2, new = svc.submit(url, mode="smart")
        assert p2.state == "RESOLVING"  # recovery path
        assert p2.id == p.id
