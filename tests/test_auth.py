"""Tests for security.auth — service token verification and user context parsing."""

import asyncio
import base64
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from models.schemas import UserContext
from security.auth import parse_user_context, verify_service_token


# ---------------------------------------------------------------------------
# verify_service_token
# ---------------------------------------------------------------------------

class TestVerifyServiceToken:
    def test_valid_token_passes(self, mock_settings):
        """Correct token should return the token string."""
        with patch("security.auth.settings", mock_settings):
            request = MagicMock()
            credentials = MagicMock()
            credentials.credentials = mock_settings.SERVICE_TOKEN_SECRET

            result = asyncio.run(verify_service_token(request, credentials))
            assert result == mock_settings.SERVICE_TOKEN_SECRET

    def test_invalid_token_raises_401(self, mock_settings):
        """Wrong token should raise HTTP 401."""
        with patch("security.auth.settings", mock_settings):
            request = MagicMock()
            credentials = MagicMock()
            credentials.credentials = "wrong-token"

            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(verify_service_token(request, credentials))
            assert exc_info.value.status_code == 401

    def test_uses_hmac_compare_digest(self, mock_settings):
        """Token comparison must use hmac.compare_digest (timing-safe)."""
        with patch("security.auth.settings", mock_settings), \
             patch("hmac.compare_digest", return_value=True) as mock_cmp:
            request = MagicMock()
            credentials = MagicMock()
            credentials.credentials = "anything"

            asyncio.run(verify_service_token(request, credentials))
            mock_cmp.assert_called_once()


# ---------------------------------------------------------------------------
# parse_user_context
# ---------------------------------------------------------------------------

class TestParseUserContext:
    def test_valid_base64_header(self, mock_user_context, user_context_header):
        """Valid base64 JSON should parse into a UserContext."""
        request = MagicMock()
        request.headers = {"X-User-Context": user_context_header}

        ctx = parse_user_context(request)
        assert isinstance(ctx, UserContext)
        assert ctx.user_id == mock_user_context.user_id
        assert ctx.source_system == mock_user_context.source_system

    def test_invalid_base64_raises_400(self):
        """Garbage base64 should raise HTTP 400."""
        request = MagicMock()
        request.headers = {"X-User-Context": "not-valid-base64!!!"}

        with pytest.raises(HTTPException) as exc_info:
            parse_user_context(request)
        assert exc_info.value.status_code == 400

    def test_missing_header_raises_400(self):
        """Missing X-User-Context header should raise HTTP 400."""
        request = MagicMock()
        request.headers = MagicMock()
        request.headers.get = MagicMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            parse_user_context(request)
        assert exc_info.value.status_code == 400

    def test_valid_json_but_wrong_schema_raises_400(self):
        """Valid base64 JSON that doesn't match UserContext schema should raise 400."""
        payload = base64.b64encode(json.dumps({"foo": "bar"}).encode()).decode()
        request = MagicMock()
        request.headers = {"X-User-Context": payload}

        with pytest.raises(HTTPException) as exc_info:
            parse_user_context(request)
        assert exc_info.value.status_code == 400

    def test_minimal_user_context(self):
        """Only user_id is truly required; defaults should fill the rest."""
        payload = base64.b64encode(json.dumps({"user_id": 99}).encode()).decode()
        request = MagicMock()
        request.headers = {"X-User-Context": payload}

        ctx = parse_user_context(request)
        assert ctx.user_id == 99
        assert ctx.locale == "it"  # default
