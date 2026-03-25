"""Rate limiting — per-user daily limits and global concurrency control."""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import HTTPException, status

from config import settings
from db.sqlite_db import get_db

logger = logging.getLogger(__name__)

# Global concurrency control for CLI calls
_cli_lock = asyncio.Lock()
_cli_active = 0


async def acquire_cli_slot():
    """Acquire a CLI execution slot. Raises 429 if all slots busy.

    H3 fix: use an atomic counter under Lock instead of semaphore + wait_for(0.0).
    This avoids the TOCTOU race and the wait_for coroutine-wrapping issue.
    """
    global _cli_active
    async with _cli_lock:
        if _cli_active >= settings.MAX_CONCURRENT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="AI service is busy. Please try again in a moment.",
            )
        _cli_active += 1


async def release_cli_slot():
    """Release a CLI execution slot."""
    global _cli_active
    async with _cli_lock:
        _cli_active = max(0, _cli_active - 1)


def check_daily_limit(user_id: int, source_system: str):
    """Check if user has exceeded daily request limit."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as db:
        row = db.execute(
            """SELECT COUNT(*) as cnt FROM usage_log
               WHERE user_id = ? AND source_system = ? AND created_at >= ?""",
            (user_id, source_system, today),
        ).fetchone()

    if row and row["cnt"] >= settings.MAX_REQUESTS_PER_USER_PER_DAY:
        logger.warning(f"User {user_id} hit daily limit ({row['cnt']} requests)")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily request limit reached ({settings.MAX_REQUESTS_PER_USER_PER_DAY}). Try again tomorrow.",
        )
