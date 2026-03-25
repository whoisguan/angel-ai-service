"""Shared pytest fixtures for angel-ai-service tests."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from models.schemas import UserContext, UserScope


@pytest.fixture
def mock_settings():
    """Provide a mock Settings object with safe defaults."""
    s = MagicMock()
    s.SERVICE_TOKEN_SECRET = "test-secret-token"
    s.CLAUDE_CLI_PATH = "claude"
    s.CLAUDE_MODEL = "sonnet"
    s.CLAUDE_MAX_BUDGET_USD = 0.50
    s.CLAUDE_PERMISSION_MODE = "bypassPermissions"
    s.CLAUDE_TIMEOUT_SECONDS = 120
    s.MCP_SERVER_SCRIPT = "/fake/mcp_server.py"
    s.MCP_PYTHON_PATH = "python"
    s.KPI_DATABASE_URL = "mysql+pymysql://test:test@localhost/test"
    s.BI_SQLSERVER_URL = ""
    s.MAX_REQUESTS_PER_USER_PER_DAY = 100
    s.MAX_CONCURRENT_REQUESTS = 3
    s.DAILY_LIMIT_TIMEZONE = "Europe/Rome"
    s.SQLITE_DB_PATH = ":memory:"
    return s


@pytest.fixture
def mock_user_context() -> UserContext:
    """Provide a standard UserContext for tests."""
    return UserContext(
        user_id=42,
        source_system="angel-kpi",
        roles=["store_manager"],
        permissions=["view_kpi", "view_bonus"],
        scope=UserScope(store_ids=[1, 2, 3], department_codes=["SALES"]),
        locale="it",
    )


@pytest.fixture
def user_context_header(mock_user_context: UserContext) -> str:
    """Return a base64-encoded X-User-Context header value."""
    payload = mock_user_context.model_dump()
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")


@pytest.fixture
def mock_request(user_context_header):
    """Return a fake FastAPI Request with valid headers and credentials."""
    request = MagicMock()
    request.headers = {"X-User-Context": user_context_header}
    return request
