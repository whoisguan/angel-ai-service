"""Role to Gemini model routing.

Given the role names a request arrives with, returns the Gemini model
identifier to use. Multiple roles are resolved by priority DESC (the
highest-priority mapping wins). Unmapped roles fall back to the default
model configured via GEMINI_MODEL in .env.

Trust boundary: role_names must originate from a source authenticated
at the service-to-service boundary (X-Service-Token on /chat). This
module does not validate the roles themselves.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from config import settings
from db.sqlite_db import get_db


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_model_for_roles(role_names: list[str]) -> str:
    if not role_names:
        return settings.GEMINI_MODEL
    placeholders = ",".join("?" * len(role_names))
    query = (
        f"SELECT model FROM role_model_map "
        f"WHERE role_name IN ({placeholders}) "
        f"ORDER BY priority DESC, id ASC LIMIT 1"
    )
    with get_db() as conn:
        row = conn.execute(query, role_names).fetchone()
    return row["model"] if row else settings.GEMINI_MODEL


def list_mappings() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, role_name, model, priority, updated_at, updated_by
               FROM role_model_map
               ORDER BY priority DESC, role_name"""
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_mappings(items: list[dict], updated_by: Optional[str]) -> int:
    """Batch upsert by role_name. Returns the number of rows applied."""
    if not items:
        return 0
    now = _now_iso()
    applied = 0
    with get_db() as conn:
        for it in items:
            conn.execute(
                """INSERT INTO role_model_map
                     (role_name, model, priority, updated_at, updated_by)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(role_name) DO UPDATE SET
                     model = excluded.model,
                     priority = excluded.priority,
                     updated_at = excluded.updated_at,
                     updated_by = excluded.updated_by""",
                (
                    it["role_name"],
                    it["model"],
                    int(it.get("priority", 0)),
                    now,
                    updated_by,
                ),
            )
            applied += 1
    return applied


def delete_mapping(role_name: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM role_model_map WHERE role_name = ?", (role_name,)
        )
        return cur.rowcount > 0
