"""Tool registry for KPI/BI read-only queries.

Gemini backend uses function calling. This module provides:
- Gemini function declarations (JSON schema)
- A safe tool runner that enforces per-request store scope

Tools reuse the existing implementations in mcp_server.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

from config import settings
from mcp_server import (
    reset_runtime_context,
    set_runtime_context,
    tool_detect_anomalies,
    tool_explain_calculation,
    tool_get_config_params,
    tool_get_department_bonus,
    tool_get_employee_bonus,
    tool_get_employee_score,
    tool_get_score_trend,
    tool_get_scoring_completion,
    tool_get_store_performance,
    tool_get_store_ranking,
)


GEMINI_FUNCTION_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "get_store_performance",
        "description": "Store performance (revenue, growth, KPI score, bonus pool).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "store_id": {"type": "INTEGER", "description": "Optional store id."},
                "year": {"type": "INTEGER", "description": "Year, e.g. 2026."},
                "month": {"type": "INTEGER", "description": "Optional month 1-12."},
            },
            "required": ["year"],
        },
    },
    {
        "name": "get_employee_bonus",
        "description": "Employee bonus details (scoped to permitted stores).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "employee_id": {"type": "INTEGER", "description": "Optional employee id."},
                "year": {"type": "INTEGER", "description": "Year, e.g. 2026."},
                "month": {"type": "INTEGER", "description": "Month 1-12."},
            },
            "required": ["year", "month"],
        },
    },
    {
        "name": "get_store_ranking",
        "description": "Store ranking by KPI score for a month (scoped).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "year": {"type": "INTEGER"},
                "month": {"type": "INTEGER"},
            },
            "required": ["year", "month"],
        },
    },
    {
        "name": "get_employee_score",
        "description": "Employee KPI score records (scoped).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "employee_id": {"type": "INTEGER"},
                "store_id": {"type": "INTEGER"},
                "year": {"type": "INTEGER"},
                "month": {"type": "INTEGER"},
            },
            "required": ["year", "month"],
        },
    },
    {
        "name": "get_score_trend",
        "description": "Score trend over months for STORE or EMPLOYEE (scoped).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "entity_type": {"type": "STRING", "description": "STORE or EMPLOYEE."},
                "entity_id": {"type": "INTEGER"},
                "year": {"type": "INTEGER"},
                "month_from": {"type": "INTEGER", "description": "Start month, default 1."},
                "month_to": {"type": "INTEGER", "description": "End month, default 12."},
            },
            "required": ["entity_type", "entity_id", "year"],
        },
    },
    {
        "name": "get_scoring_completion",
        "description": "Scoring task completion status (scoped).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "year": {"type": "INTEGER"},
                "month": {"type": "INTEGER"},
                "store_id": {"type": "INTEGER"},
            },
            "required": ["year", "month"],
        },
    },
    {
        "name": "get_department_bonus",
        "description": "Department-level bonus allocation for a store (scoped).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "store_id": {"type": "INTEGER"},
                "year": {"type": "INTEGER"},
                "month": {"type": "INTEGER"},
            },
            "required": ["store_id", "year", "month"],
        },
    },
    {
        "name": "get_config_params",
        "description": "Public configuration parameters (no store scope).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "param_category": {"type": "STRING"},
            },
        },
    },
    {
        "name": "explain_calculation",
        "description": "Explain a calculation topic (pre-defined bilingual explanation).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "topic": {"type": "STRING"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "detect_anomalies",
        "description": "Detect anomalies in monthly settlement (scoped).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "year": {"type": "INTEGER"},
                "month": {"type": "INTEGER"},
            },
            "required": ["year", "month"],
        },
    },
]


_TOOL_MAP = {
    "get_store_performance": tool_get_store_performance,
    "get_employee_bonus": tool_get_employee_bonus,
    "get_store_ranking": tool_get_store_ranking,
    "get_employee_score": tool_get_employee_score,
    "get_score_trend": tool_get_score_trend,
    "get_scoring_completion": tool_get_scoring_completion,
    "get_department_bonus": tool_get_department_bonus,
    "get_config_params": tool_get_config_params,
    "explain_calculation": tool_explain_calculation,
    "detect_anomalies": tool_detect_anomalies,
}


async def run_tool(name: str, args: dict, user_store_ids: list[int] | None) -> dict:
    """Execute a tool safely with per-request scope enforced."""
    fn = _TOOL_MAP.get(name)
    if not fn:
        return {"error": f"Unknown tool: {name}"}
    if not isinstance(args, dict):
        return {"error": "Invalid tool args (expected object)."}

    tokens = set_runtime_context(user_store_ids, settings.KPI_DATABASE_URL)
    try:
        return await asyncio.to_thread(fn, args)
    finally:
        reset_runtime_context(tokens)

