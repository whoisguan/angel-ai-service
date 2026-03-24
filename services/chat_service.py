"""Chat service — orchestrates CLI calls, history, and sanitization."""

import os
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import claude_cli
from config import settings
from db.sqlite_db import get_db
from models.schemas import ChatRequest, ChatMessage, UserContext
from security.sanitizer import sanitize_output


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


async def chat(
    request: ChatRequest,
    user_ctx: UserContext,
) -> ChatMessage:
    """Process a non-streaming chat request."""
    conversation_id = request.conversation_id or claude_cli.generate_conversation_id()
    user_msg_id = claude_cli.generate_message_id()
    ai_msg_id = claude_cli.generate_message_id()

    # Build prompt with conversation history
    history = []
    if request.conversation_id:
        history = _get_conversation_history(request.conversation_id)

    # Build the full prompt (history + current question)
    prompt_parts = []
    for msg in history[-8:]:  # last 8 turns
        role_label = "User" if msg["role"] == "user" else "Assistant"
        prompt_parts.append(f"{role_label}: {msg['content']}")
    prompt_parts.append(f"User: {request.message}")
    full_prompt = "\n\n".join(prompt_parts)

    system_prompt = _load_system_prompt(user_ctx, request.page_context)

    # Save user message
    _save_message(conversation_id, user_msg_id, "user", request.message, user_ctx)

    # Call Claude CLI
    result = await claude_cli.query(
        prompt=full_prompt,
        system_prompt=system_prompt,
        user_store_ids=user_ctx.scope.store_ids,
    )

    # Sanitize output
    clean_content = sanitize_output(result.result)

    # Save assistant message
    _save_message(
        conversation_id, ai_msg_id, "assistant", clean_content, user_ctx,
        cost_usd=result.cost_usd, duration_ms=result.duration_ms, model=result.model,
    )

    return ChatMessage(
        message_id=ai_msg_id,
        role="assistant",
        content=clean_content,
        timestamp=datetime.now(timezone.utc),
        cost_usd=result.cost_usd,
        duration_ms=result.duration_ms,
    )


async def chat_stream(
    request: ChatRequest,
    user_ctx: UserContext,
) -> AsyncIterator[str]:
    """Process a streaming chat request, yielding SSE events."""
    conversation_id = request.conversation_id or claude_cli.generate_conversation_id()
    user_msg_id = claude_cli.generate_message_id()
    ai_msg_id = claude_cli.generate_message_id()

    # Build prompt
    history = []
    if request.conversation_id:
        history = _get_conversation_history(request.conversation_id)

    prompt_parts = []
    for msg in history[-8:]:
        role_label = "User" if msg["role"] == "user" else "Assistant"
        prompt_parts.append(f"{role_label}: {msg['content']}")
    prompt_parts.append(f"User: {request.message}")
    full_prompt = "\n\n".join(prompt_parts)

    system_prompt = _load_system_prompt(user_ctx, request.page_context)

    # Save user message
    _save_message(conversation_id, user_msg_id, "user", request.message, user_ctx)

    # Emit metadata event
    import json
    yield f"data: {json.dumps({'type': 'metadata', 'conversation_id': conversation_id, 'message_id': ai_msg_id})}\n\n"

    full_content = ""
    cost_usd = 0
    duration_ms = 0
    model = ""

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

    # Sanitize final content
    clean_content = sanitize_output(full_content)

    # Save assistant message
    _save_message(
        conversation_id, ai_msg_id, "assistant", clean_content, user_ctx,
        cost_usd=cost_usd, duration_ms=duration_ms, model=model,
    )

    # Done event
    yield f"data: {json.dumps({'type': 'done', 'message_id': ai_msg_id, 'cost_usd': cost_usd, 'duration_ms': duration_ms})}\n\n"
