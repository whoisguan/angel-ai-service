"""Tests for mcp_server.handle_request — JSON-RPC protocol correctness.

These tests do NOT connect to any database. They only verify the MCP protocol
envelope (initialize, tools/list, tools/call routing, ping, unknown method).
"""

from unittest.mock import patch, MagicMock

import pytest


# We need to mock database-related imports before importing mcp_server
@pytest.fixture(autouse=True)
def _patch_mcp_imports(monkeypatch):
    """Patch heavy imports so mcp_server can be loaded without a real DB."""
    import sys
    # Provide stub modules if they aren't importable
    dummy = MagicMock()
    for mod in ("pymysql", "pyodbc", "sqlalchemy", "sqlalchemy.text"):
        if mod not in sys.modules:
            monkeypatch.setitem(sys.modules, mod, dummy)


def _get_handle_request():
    """Import handle_request lazily (after patching)."""
    from mcp_server import handle_request
    return handle_request


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_initialize_returns_capabilities(self):
        handle_request = _get_handle_request()
        resp = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        result = resp["result"]
        assert "protocolVersion" in result
        assert "capabilities" in result
        assert "serverInfo" in result
        assert result["serverInfo"]["name"] == "kpi-data"

    def test_notifications_initialized_returns_none(self):
        handle_request = _get_handle_request()
        resp = handle_request({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        assert resp is None


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------

class TestToolsList:
    def test_tools_list_returns_tools(self):
        handle_request = _get_handle_request()
        resp = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert resp["id"] == 2
        tools = resp["result"]["tools"]
        assert isinstance(tools, list)
        assert len(tools) > 0
        # Each tool should have name, description, inputSchema
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool

    def test_tools_list_contains_expected_tools(self):
        handle_request = _get_handle_request()
        resp = handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        names = {t["name"] for t in resp["result"]["tools"]}
        assert "get_store_performance" in names
        assert "get_employee_bonus" in names
        assert "get_store_ranking" in names


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------

class TestToolsCall:
    def test_unknown_tool_returns_error(self):
        handle_request = _get_handle_request()
        resp = handle_request({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        })
        assert resp["id"] == 4
        assert resp["result"]["isError"] is True
        assert "Unknown tool" in resp["result"]["content"][0]["text"]

    def test_valid_tool_call_with_mock_handler(self):
        """Patch a tool handler to avoid real DB access, verify envelope."""
        handle_request = _get_handle_request()
        from mcp_server import TOOLS

        original_handler = TOOLS["get_store_performance"]["handler"]
        try:
            TOOLS["get_store_performance"]["handler"] = lambda args: {"rows": [], "count": 0}
            resp = handle_request({
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "get_store_performance", "arguments": {"year": 2026}},
            })
            assert resp["id"] == 5
            assert "isError" not in resp["result"] or resp["result"].get("isError") is not True
            assert resp["result"]["content"][0]["type"] == "text"
        finally:
            TOOLS["get_store_performance"]["handler"] = original_handler

    def test_tool_handler_exception_returns_error(self):
        """If the handler raises, the response should wrap the error."""
        handle_request = _get_handle_request()
        from mcp_server import TOOLS

        original_handler = TOOLS["get_store_performance"]["handler"]
        try:
            TOOLS["get_store_performance"]["handler"] = lambda args: (_ for _ in ()).throw(
                ValueError("db gone")
            )
            resp = handle_request({
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "get_store_performance", "arguments": {"year": 2026}},
            })
            assert resp["result"]["isError"] is True
            assert "Error" in resp["result"]["content"][0]["text"]
        finally:
            TOOLS["get_store_performance"]["handler"] = original_handler


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------

class TestPing:
    def test_ping_returns_empty_result(self):
        handle_request = _get_handle_request()
        resp = handle_request({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}})
        assert resp["id"] == 7
        assert resp["result"] == {}


# ---------------------------------------------------------------------------
# unknown method
# ---------------------------------------------------------------------------

class TestUnknownMethod:
    def test_unknown_method_returns_error_code(self):
        handle_request = _get_handle_request()
        resp = handle_request({"jsonrpc": "2.0", "id": 8, "method": "bogus/method", "params": {}})
        assert resp["id"] == 8
        assert "error" in resp
        assert resp["error"]["code"] == -32601
