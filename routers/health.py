"""Health check and admin endpoints."""

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from config import settings
from db.sqlite_db import get_db
from models.schemas import HealthResponse, UsageStats, PromptVersionCreate, PromptVersionResponse
from security.auth import verify_service_token

router = APIRouter(tags=["admin"])

_start_time = time.time()
_cli_status_cache: dict = {"ok": False, "checked_at": 0}
_CLI_CHECK_INTERVAL = 60  # seconds


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check — no authentication required. CLI check cached for 60s."""
    now = time.time()

    if now - _cli_status_cache["checked_at"] > _CLI_CHECK_INTERVAL:
        try:
            import subprocess
            def _check_cli():
                return subprocess.run(
                    [settings.CLAUDE_CLI_PATH, "--version"],
                    capture_output=True, timeout=10,
                )
            result = await asyncio.to_thread(_check_cli)
            _cli_status_cache["ok"] = result.returncode == 0
        except Exception:
            _cli_status_cache["ok"] = False
        _cli_status_cache["checked_at"] = now

    return HealthResponse(
        status="healthy" if _cli_status_cache["ok"] else "degraded",
        version="0.1.0",
        cli_available=_cli_status_cache["ok"],
        uptime_seconds=int(now - _start_time),
    )


@router.get("/api/ai/usage", response_model=UsageStats)
async def get_usage(
    period: str = "day",
    _token: str = Depends(verify_service_token),
):
    """Get usage statistics. Requires service token."""
    if period == "day":
        date_filter = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    elif period == "week":
        # Last 7 days
        from datetime import timedelta
        date_filter = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    elif period == "month":
        date_filter = datetime.now(timezone.utc).strftime("%Y-%m-01")
    else:
        date_filter = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with get_db() as db:
        row = db.execute(
            """SELECT
                COUNT(*) as total_requests,
                COALESCE(SUM(cost_usd), 0) as total_cost_usd,
                COALESCE(AVG(duration_ms), 0) as avg_duration_ms,
                COUNT(DISTINCT user_id) as unique_users
            FROM usage_log
            WHERE created_at >= ?""",
            (date_filter,),
        ).fetchone()

    return UsageStats(
        period=date_filter,
        total_requests=row["total_requests"],
        total_cost_usd=round(row["total_cost_usd"], 4),
        avg_duration_ms=int(row["avg_duration_ms"]),
        unique_users=row["unique_users"],
    )


# ---------------------------------------------------------------------------
# Prompt Version Management (admin only)
# ---------------------------------------------------------------------------

@router.get("/api/ai/prompts", response_model=list[PromptVersionResponse])
async def list_prompts(
    _token: str = Depends(verify_service_token),
):
    """List all prompt versions."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, version_tag, description, is_active, created_at, created_by FROM prompt_versions ORDER BY created_at DESC"
        ).fetchall()
    return [
        PromptVersionResponse(
            id=r["id"], version_tag=r["version_tag"], description=r["description"],
            is_active=bool(r["is_active"]), created_at=r["created_at"], created_by=r["created_by"],
        )
        for r in rows
    ]


@router.post("/api/ai/prompts", response_model=PromptVersionResponse, status_code=201)
async def create_prompt(
    body: PromptVersionCreate,
    _token: str = Depends(verify_service_token),
):
    """Create a new prompt version. Optionally activate it."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        # Check duplicate version_tag
        existing = db.execute(
            "SELECT id FROM prompt_versions WHERE version_tag = ?", (body.version_tag,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Version tag '{body.version_tag}' already exists")

        if body.activate:
            # Transactional: deactivate all, then activate new one
            db.execute("UPDATE prompt_versions SET is_active = 0 WHERE is_active = 1")

        cursor = db.execute(
            """INSERT INTO prompt_versions (version_tag, content, description, is_active, created_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (body.version_tag, body.content, body.description, 1 if body.activate else 0, now, "admin"),
        )
        new_id = cursor.lastrowid

    return PromptVersionResponse(
        id=new_id, version_tag=body.version_tag, description=body.description,
        is_active=body.activate, created_at=now, created_by="admin",
    )


@router.put("/api/ai/prompts/{prompt_id}/activate")
async def activate_prompt(
    prompt_id: int,
    _token: str = Depends(verify_service_token),
):
    """Activate a specific prompt version (deactivates all others)."""
    with get_db() as db:
        target = db.execute("SELECT id, version_tag FROM prompt_versions WHERE id = ?", (prompt_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Prompt version not found")

        # Transactional: deactivate all, activate target
        db.execute("UPDATE prompt_versions SET is_active = 0 WHERE is_active = 1")
        db.execute("UPDATE prompt_versions SET is_active = 1 WHERE id = ?", (prompt_id,))

    return {"status": "activated", "version_tag": target["version_tag"]}
