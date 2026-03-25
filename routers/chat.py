"""Chat API router — SSE streaming and synchronous endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from claude_cli import CLIError
from db.sqlite_db import get_db
from models.schemas import ChatRequest, ChatResponse, FeedbackRequest, UserContext
from security.auth import get_authenticated_context, verify_service_token
from security.input_guard import check_input
from security.rate_limiter import acquire_cli_slot, release_cli_slot, check_daily_limit
from services.chat_service import chat, chat_stream

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.post("/chat")
async def chat_endpoint(
    request: ChatRequest,
    user_ctx: UserContext = Depends(get_authenticated_context),
):
    """Chat with AI. Supports both streaming (SSE) and synchronous modes.

    For streaming: pre-flight checks run here so HTTP errors (400/429) are
    returned before the response starts. The CLI slot is acquired here and
    released inside the generator's finally block.

    For sync: chat() handles everything internally.
    """
    try:
        if request.stream:
            # Pre-flight checks for streaming (must happen before first yield)
            injection = check_input(request.message)
            if injection:
                logger.warning(f"Injection attempt by user {user_ctx.user_id}: {injection}")
                raise HTTPException(status_code=400, detail="Your message was blocked by our safety filter.")

            check_daily_limit(user_ctx.user_id, user_ctx.source_system)
            await acquire_cli_slot()

            # chat_stream will call release_cli_slot in its finally block
            return StreamingResponse(
                chat_stream(request, user_ctx, slot_already_acquired=True),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            # Sync path: chat() handles all checks internally
            return await chat(request, user_ctx)

    except HTTPException:
        raise  # Let 400/429 pass through unchanged
    except CLIError as e:
        logger.error(f"CLI error: {e} | stderr: {e.stderr}")
        raise HTTPException(
            status_code=503,
            detail="AI service temporarily unavailable. Please try again.",
        )
    except Exception:
        logger.exception("Unexpected error in chat endpoint")
        raise HTTPException(
            status_code=500,
            detail="Internal server error.",
        )


@router.post("/feedback")
async def submit_feedback(
    body: FeedbackRequest,
    _token: str = Depends(verify_service_token),
):
    """Record user feedback on an AI response (enhanced with accuracy + resolved)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        db.execute(
            "INSERT INTO feedback (message_id, rating, comment, created_at) VALUES (?, ?, ?, ?)",
            (body.message_id, body.rating, body.comment, now),
        )

        # Link feedback to retrieval event via message_id (traceable)
        retrieval_linked = False
        if body.accuracy or body.resolved is not None:
            feedback_val = body.accuracy or ("resolved" if body.resolved else "unresolved")
            cursor = db.execute(
                """UPDATE retrieval_feedback
                   SET user_feedback = ?
                   WHERE message_id = ?""",
                (feedback_val, body.message_id),
            )
            retrieval_linked = cursor.rowcount > 0
            if not retrieval_linked:
                logger.warning(f"Feedback for message {body.message_id}: no matching retrieval_feedback row")

    return {"status": "recorded", "retrieval_linked": retrieval_linked if (body.accuracy or body.resolved is not None) else None}
