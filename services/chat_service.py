"""Chat service — orchestrates CLI calls, history, and sanitization."""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import claude_cli
from config import settings
from db.sqlite_db import get_db
from models.schemas import ChatRequest, ChatMessage, ChatResponse, UserContext
from security.input_guard import check_input, sanitize_page_context
from security.rate_limiter import acquire_cli_slot, release_cli_slot, check_daily_limit
from security.sanitizer import sanitize_output

logger = logging.getLogger(__name__)


def _load_system_prompt(user_ctx: UserContext, page_context: dict = None) -> str:
    """Load and personalize the system prompt."""
    prompt_path = settings.SYSTEM_PROMPT_PATH
    if os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            base_prompt = f.read()
    else:
        base_prompt = "You are an AI assistant for Angel Mercatone KPI system."

    # Inject user context
    user_info = (
        f"\n\n## Current User\n"
        f"- User ID: {user_ctx.user_id}\n"
        f"- Roles: {', '.join(user_ctx.roles)}\n"
        f"- Accessible stores: {', '.join(str(s) for s in user_ctx.scope.store_ids) or 'all'}\n"
        f"- Language preference: {user_ctx.locale}\n"
    )

    # Inject page context if available
    page_info = ""
    if page_context:
        page_info = f"\n## Current Page Context\n"
        for key, value in page_context.items():
            page_info += f"- {key}: {value}\n"

    return base_prompt + user_info + page_info


def _get_conversation_history(conversation_id: str, limit: int = 10) -> list[dict]:
    """Load recent messages from a conversation for context."""
    with get_db() as db:
        rows = db.execute(
            """SELECT role, content FROM messages
               WHERE conversation_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (conversation_id, limit),
        ).fetchall()

    # Reverse to chronological order
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _save_message(
    conversation_id: str,
    message_id: str,
    role: str,
    content: str,
    user_ctx: UserContext,
    cost_usd: float = 0,
    duration_ms: int = 0,
    model: str = "",
):
    """Save a message to the database."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        # Ensure conversation exists
        existing = db.execute(
            "SELECT id FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

        if not existing:
            db.execute(
                """INSERT INTO conversations (id, user_id, source_system, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (conversation_id, user_ctx.user_id, user_ctx.source_system, now, now),
            )
        else:
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )

        # Save message
        db.execute(
            """INSERT INTO messages (id, conversation_id, role, content, cost_usd, duration_ms, model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, conversation_id, role, content, cost_usd, duration_ms, model, now),
        )

        # Log usage for assistant messages
        if role == "assistant":
            db.execute(
                """INSERT INTO usage_log (user_id, source_system, cost_usd, duration_ms, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_ctx.user_id, user_ctx.source_system, cost_usd, duration_ms, model, now),
            )


def _build_full_prompt(message: str, conversation_id: str = None) -> str:
    """Build full prompt with conversation history."""
    history = []
    if conversation_id:
        history = _get_conversation_history(conversation_id)

    prompt_parts = []
    for msg in history[-8:]:
        role_label = "User" if msg["role"] == "user" else "Assistant"
        prompt_parts.append(f"<{role_label.lower()}>{msg['content']}</{role_label.lower()}>")
    prompt_parts.append(f"<user>{message}</user>")
    return "\n\n".join(prompt_parts)


async def chat(
    request: ChatRequest,
    user_ctx: UserContext,
) -> ChatResponse:
    """Process a non-streaming chat request. Returns ChatResponse with real conversation_id."""
    conversation_id = request.conversation_id or claude_cli.generate_conversation_id()
    user_msg_id = claude_cli.generate_message_id()
    ai_msg_id = claude_cli.generate_message_id()

    # Input validation
    injection = check_input(request.message)
    if injection:
        logger.warning(f"Injection attempt by user {user_ctx.user_id}: {injection}")

    # Rate limiting
    check_daily_limit(user_ctx.user_id, user_ctx.source_system)

    # Sanitize page context
    clean_page_ctx = sanitize_page_context(request.page_context) if request.page_context else None

    full_prompt = _build_full_prompt(request.message, request.conversation_id)
    system_prompt = _load_system_prompt(user_ctx, clean_page_ctx)

    # Save user message
    _save_message(conversation_id, user_msg_id, "user", request.message, user_ctx)

    # Call Claude CLI with concurrency control
    await acquire_cli_slot()
    try:
        result = await claude_cli.query(
            prompt=full_prompt,
            system_prompt=system_prompt,
            user_store_ids=user_ctx.scope.store_ids,
        )
    finally:
        release_cli_slot()

    # Sanitize output
    clean_content = sanitize_output(result.result)

    # Save assistant message
    _save_message(
        conversation_id, ai_msg_id, "assistant", clean_content, user_ctx,
        cost_usd=result.cost_usd, duration_ms=result.duration_ms, model=result.model,
    )

    msg = ChatMessage(
        message_id=ai_msg_id,
        role="assistant",
        content=clean_content,
        timestamp=datetime.now(timezone.utc),
        cost_usd=result.cost_usd,
        duration_ms=result.duration_ms,
    )
    return ChatResponse(conversation_id=conversation_id, message=msg)


async def chat_stream(
    request: ChatRequest,
    user_ctx: UserContext,
) -> AsyncIterator[str]:
    """Process a streaming chat request, yielding SSE events."""
    conversation_id = request.conversation_id or claude_cli.generate_conversation_id()
    user_msg_id = claude_cli.generate_message_id()
    ai_msg_id = claude_cli.generate_message_id()

    # Input validation
    injection = check_input(request.message)
    if injection:
        logger.warning(f"Injection attempt (stream) by user {user_ctx.user_id}: {injection}")

    # Rate limiting
    check_daily_limit(user_ctx.user_id, user_ctx.source_system)

    clean_page_ctx = sanitize_page_context(request.page_context) if request.page_context else None
    full_prompt = _build_full_prompt(request.message, request.conversation_id)
    system_prompt = _load_system_prompt(user_ctx, clean_page_ctx)

    # Save user message
    _save_message(conversation_id, user_msg_id, "user", request.message, user_ctx)

    # Emit metadata event
    import json
    yield f"data: {json.dumps({'type': 'metadata', 'conversation_id': conversation_id, 'message_id': ai_msg_id})}\n\n"

    full_content = ""
    cost_usd = 0
    duration_ms = 0
    model = ""

    await acquire_cli_slot()
    try:
      async for event in claude_cli.stream(
        prompt=full_prompt,
        system_prompt=system_prompt,
        user_store_ids=user_ctx.scope.store_ids,
      ):
        if event["type"] == "content":
            chunk = event["text"]
            full_content += chunk
            # Real-time PII redaction on each chunk
            clean_chunk = sanitize_output(chunk)
            yield f"data: {json.dumps({'type': 'delta', 'content': clean_chunk})}\n\n"

        elif event["type"] == "tool_use":
            yield f"data: {json.dumps({'type': 'tool_use', 'tool': event['tool']})}\n\n"

        elif event["type"] == "result":
            cost_usd = event.get("cost_usd", 0)
            duration_ms = event.get("duration_ms", 0)
            # Use result text if we didn't get streaming content
            if not full_content and event.get("text"):
                full_content = event["text"]

        elif event["type"] == "error":
            yield f"data: {json.dumps({'type': 'error', 'message': event['message']})}\n\n"

    finally:
      release_cli_slot()

    # Sanitize final content
    clean_content = sanitize_output(full_content)

    # Save assistant message
    _save_message(
        conversation_id, ai_msg_id, "assistant", clean_content, user_ctx,
        cost_usd=cost_usd, duration_ms=duration_ms, model=model,
    )

    # Done event
    yield f"data: {json.dumps({'type': 'done', 'message_id': ai_msg_id, 'cost_usd': cost_usd, 'duration_ms': duration_ms})}\n\n"
