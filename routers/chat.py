"""Chat API router — SSE streaming and synchronous endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from claude_cli import CLIError
from models.schemas import ChatRequest, ChatResponse, UserContext
from security.auth import get_authenticated_context
from services.chat_service import chat, chat_stream

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.post("/chat")
async def chat_endpoint(
    request: ChatRequest,
    user_ctx: UserContext = Depends(get_authenticated_context),
):
    """Chat with AI. Supports both streaming (SSE) and synchronous modes.

    - stream=true (default): Returns SSE stream with delta events
    - stream=false: Returns complete JSON response
    """
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
            message = await chat(request, user_ctx)
            return ChatResponse(
                conversation_id=request.conversation_id or "new",
                message=message,
            )

    except CLIError as e:
        import logging
        logging.getLogger(__name__).error(f"CLI error: {e} | stderr: {e.stderr}")
        raise HTTPException(
            status_code=503,
            detail="AI service temporarily unavailable. Please try again.",
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Unexpected error in chat endpoint")
        raise HTTPException(
            status_code=500,
            detail="Internal server error.",
        )
