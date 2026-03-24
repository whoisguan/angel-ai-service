"""Health check and admin endpoints."""

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from config import settings
from db.sqlite_db import get_db
from models.schemas import HealthResponse, UsageStats
from security.auth import verify_service_token

router = APIRouter(tags=["admin"])

_start_time = time.time()


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check — no authentication required."""
    # Check CLI availability
    cli_ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.CLAUDE_CLI_PATH, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        cli_ok = proc.returncode == 0
    except Exception:
        cli_ok = False

    return HealthResponse(
        status="healthy" if cli_ok else "degraded",
        version="0.1.0",
        cli_available=cli_ok,
        uptime_seconds=int(time.time() - _start_time),
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
