"""Tests for models.schemas — Pydantic validation for API payloads."""

import pytest
from pydantic import ValidationError

from models.schemas import ChatRequest, FeedbackRequest, UserContext, UserScope


# ---------------------------------------------------------------------------
# ChatRequest
# ---------------------------------------------------------------------------

class TestChatRequest:
    def test_valid_message(self):
        req = ChatRequest(message="Qual e' il fatturato?")
        assert req.message == "Qual e' il fatturato?"
        assert req.stream is True  # default

    def test_empty_message_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="")

    def test_too_long_message_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="x" * 4001)

    def test_max_length_message_accepted(self):
        req = ChatRequest(message="x" * 4000)
        assert len(req.message) == 4000


# ---------------------------------------------------------------------------
# UserContext
# ---------------------------------------------------------------------------

class TestUserContext:
    def test_minimal_user_context(self):
        ctx = UserContext(user_id=1)
        assert ctx.user_id == 1
        assert ctx.source_system == "angel-kpi"
        assert ctx.locale == "it"
        assert ctx.roles == []
        assert ctx.scope.store_ids is None  # None = admin (all stores)

    def test_full_user_context(self):
        ctx = UserContext(
            user_id=42,
            source_system="odoo",
            roles=["admin"],
            permissions=["all"],
            scope=UserScope(store_ids=[1, 2], department_codes=["HR"]),
            locale="en",
        )
        assert ctx.scope.store_ids == [1, 2]
        assert ctx.locale == "en"


# ---------------------------------------------------------------------------
# FeedbackRequest
# ---------------------------------------------------------------------------

class TestFeedbackRequest:
    def test_valid_ratings(self):
        for rating in ("helpful", "not_helpful", "wrong", "harmful"):
            fb = FeedbackRequest(message_id="msg_abc123", rating=rating)
            assert fb.rating == rating

    def test_invalid_rating_rejected(self):
        with pytest.raises(ValidationError):
            FeedbackRequest(message_id="msg_abc123", rating="excellent")

    def test_optional_comment(self):
        fb = FeedbackRequest(message_id="msg_abc123", rating="helpful", comment="Great!")
        assert fb.comment == "Great!"
