"""Chat API router — SSE streaming and synchronous endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from claude_cli import CLIError
from db.sqlite_db import get_db
from models.schemas import ChatRequest, ChatResponse, FeedbackRequest, UserContext
from security.auth import get_authenticated_context, verify_service_token
from services.chat_service import chat, chat_stream

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.post("/chat")
async def chat_endpoint(
    request: ChatRequest,
    user_ctx: UserContext = Depends(get_authenticated_context),
):
    """Chat with AI. Supports both streaming (SSE) and synchronous modes."""
    try:
        if request.stream:
            return StreamingResponse(
                chat_stream(request, user_ctx),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            return await chat(request, user_ctx)

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
    """Record user feedback on an AI response."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        db.execute(
            "INSERT INTO feedback (message_id, rating, comment, created_at) VALUES (?, ?, ?, ?)",
            (body.message_id, body.rating, body.comment, now),
        )
    return {"status": "recorded"}
