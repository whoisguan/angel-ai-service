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
from security.rate_limiter import acquire_cli_slot, release_cli_slot, check_daily_limit  # release is now async
from security.sanitizer import sanitize_output
from services.knowledge_service import build_knowledge_context, route_question, log_retrieval
from services.memory_service import build_memory_context, extract_memories_from_response
from services.user_profile_service import get_profile_summary, update_profile

logger = logging.getLogger(__name__)


def _load_system_prompt(user_ctx: UserContext, page_context: dict = None) -> tuple[str, str | None]:
    """Load and personalize the system prompt.

    Priority: DB active version > file on disk > hardcoded fallback.
    Returns (full_prompt, version_tag | None).
    """
    base_prompt = None
    version_tag = None

    # Try loading from prompt_versions table (active version)
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT version_tag, content FROM prompt_versions WHERE is_active = 1 LIMIT 1"
            ).fetchone()
            if row:
                base_prompt = row["content"]
                version_tag = row["version_tag"]
    except Exception:
        logger.debug("Failed to load prompt from DB, falling back to file")

    # Fallback to file
    if base_prompt is None:
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
        f"- Accessible stores: {', '.join(str(s) for s in user_ctx.scope.store_ids) if user_ctx.scope.store_ids else 'all (admin)'}\n"
        f"- Language preference: {user_ctx.locale}\n"
    )

    # Inject page context if available
    page_info = ""
    if page_context:
        page_info = f"\n## Current Page Context\n"
        for key, value in page_context.items():
            page_info += f"- {key}: {value}\n"

    # Language instruction based on page_context.lang or user locale
    ui_lang = (page_context or {}).get("lang") or user_ctx.locale or "it"
    lang_instruction = ""
    if ui_lang == "zh":
        lang_instruction = "\n\n**IMPORTANT: The user's interface is in Chinese. Always respond in Chinese (中文).**\n"
    else:
        lang_instruction = "\n\n**IMPORTANT: The user's interface is in Italian. Always respond in Italian (Italiano).**\n"

    # Inject user profile summary (if enough history)
    profile_info = ""
    profile_summary = get_profile_summary(user_ctx.user_id, user_ctx.source_system)
    if profile_summary:
        profile_info = f"\n## User Profile\n{profile_summary}\n"

    # Inject cross-conversation memories
    memory_info = build_memory_context(user_ctx.user_id, user_ctx.source_system) or ""

    return base_prompt + user_info + page_info + lang_instruction + profile_info + memory_info, version_tag


def _enrich_prompt_with_knowledge(prompt: str, user_message: str, user_ctx: UserContext = None) -> tuple[str, str, list[int]]:
    """Inject relevant knowledge context into the prompt if available.

    Returns (enriched_prompt, route_decision, matched_kb_ids).
    """
    user_roles = user_ctx.roles if user_ctx else None
    knowledge_ctx, route, kb_ids = build_knowledge_context(user_message, user_roles=user_roles)
    if knowledge_ctx:
        return prompt + knowledge_ctx, route, kb_ids
    return prompt, route, kb_ids


def _get_conversation_history(conversation_id: str, user_id: int, source_system: str = "angel-kpi", limit: int = 10) -> list[dict]:
    """Load recent messages from a conversation for context. Validates ownership + source_system."""
    with get_db() as db:
        # H1 fix: verify conversation belongs to requesting user AND source_system
        owner = db.execute(
            "SELECT user_id, source_system FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if owner and (owner["user_id"] != user_id or owner["source_system"] != source_system):
            logger.warning(f"User {user_id}/{source_system} attempted to access conversation {conversation_id}")
            return []

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
    """Save a message to the database. Validates conversation ownership."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        # Ensure conversation exists and belongs to this user (H1 write-path fix)
        existing = db.execute(
            "SELECT id, user_id, source_system FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

        if not existing:
            db.execute(
                """INSERT INTO conversations (id, user_id, source_system, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (conversation_id, user_ctx.user_id, user_ctx.source_system, now, now),
            )
        elif existing["user_id"] != user_ctx.user_id or existing["source_system"] != user_ctx.source_system:
            # Block cross-user or cross-system write
            logger.warning(f"User {user_ctx.user_id}/{user_ctx.source_system} tried to write to conversation {conversation_id}")
            raise ValueError(f"Conversation ownership mismatch")
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


def _build_full_prompt(message: str, conversation_id: str = None, user_id: int = None, source_system: str = "angel-kpi") -> str:
    """Build full prompt with conversation history."""
    history = []
    if conversation_id and user_id is not None:
        history = _get_conversation_history(conversation_id, user_id, source_system)

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

    # Input validation — block if injection detected (C1 fix)
    injection = check_input(request.message, user_id=user_ctx.user_id, source_system=user_ctx.source_system)
    if injection:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Your message was blocked by our safety filter.")

    # Rate limiting
    check_daily_limit(user_ctx.user_id, user_ctx.source_system)

    # Sanitize page context
    clean_page_ctx = sanitize_page_context(request.page_context) if request.page_context else None

    full_prompt = _build_full_prompt(request.message, request.conversation_id, user_ctx.user_id, user_ctx.source_system)
    system_prompt, prompt_version = _load_system_prompt(user_ctx, clean_page_ctx)
    system_prompt, route, kb_ids = _enrich_prompt_with_knowledge(system_prompt, request.message, user_ctx)

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
        await release_cli_slot()

    # Sanitize output
    clean_content = sanitize_output(result.result)

    # Save assistant message
    _save_message(
        conversation_id, ai_msg_id, "assistant", clean_content, user_ctx,
        cost_usd=result.cost_usd, duration_ms=result.duration_ms, model=result.model,
    )

    # Log retrieval with message_id for traceability
    log_retrieval(request.message, kb_ids, route, prompt_version=prompt_version, message_id=ai_msg_id)

    # Update user profile and extract memories (fire-and-forget, non-blocking)
    try:
        update_profile(user_ctx.user_id, request.message, user_ctx.source_system)
        extract_memories_from_response(user_ctx.user_id, request.message, clean_content, user_ctx.source_system, conversation_id)
    except Exception:
        logger.debug("Profile/memory update failed", exc_info=True)

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
    slot_already_acquired: bool = False,
) -> AsyncIterator[str]:
    """Process a streaming chat request, yielding SSE events.

    Pre-flight checks (injection, rate limit, slot) are done in the router
    BEFORE the generator starts, so HTTP 400/429 can be returned properly.
    The slot is released in the finally block below.
    """
    conversation_id = request.conversation_id or claude_cli.generate_conversation_id()
    user_msg_id = claude_cli.generate_message_id()
    ai_msg_id = claude_cli.generate_message_id()

    if not slot_already_acquired:
        # Fallback: if called without pre-flight from router
        injection = check_input(request.message)
        if injection:
            logger.warning(f"Injection attempt (stream) by user {user_ctx.user_id}: {injection}")
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Your message was blocked by our safety filter.")
        check_daily_limit(user_ctx.user_id, user_ctx.source_system)
        await acquire_cli_slot()

    clean_page_ctx = sanitize_page_context(request.page_context) if request.page_context else None
    full_prompt = _build_full_prompt(request.message, request.conversation_id, user_ctx.user_id, user_ctx.source_system)
    system_prompt, prompt_version = _load_system_prompt(user_ctx, clean_page_ctx)
    system_prompt, route, kb_ids = _enrich_prompt_with_knowledge(system_prompt, request.message, user_ctx)

    # Save user message
    _save_message(conversation_id, user_msg_id, "user", request.message, user_ctx)

    # Emit metadata event
    import json
    yield f"data: {json.dumps({'type': 'metadata', 'conversation_id': conversation_id, 'message_id': ai_msg_id})}\n\n"

    full_content = ""
    cost_usd = 0
    duration_ms = 0
    model = settings.CLAUDE_MODEL  # M9 fix: default model for streaming

    try:
        # H4+H5 fix: all streaming logic + save in single try/finally
        # Heartbeat: send SSE comment every 15s to keep Cloudflare alive
        import time
        last_event_time = time.monotonic()

        async for event in claude_cli.stream(
            prompt=full_prompt,
            system_prompt=system_prompt,
            user_store_ids=user_ctx.scope.store_ids,
        ):
            now_mono = time.monotonic()
            if now_mono - last_event_time > 15:
                yield ": keepalive\n\n"
            last_event_time = now_mono

            if event["type"] == "content":
                chunk = event["text"]
                full_content += chunk
                clean_chunk = sanitize_output(chunk)
                yield f"data: {json.dumps({'type': 'delta', 'content': clean_chunk})}\n\n"

            elif event["type"] == "tool_use":
                yield f"data: {json.dumps({'type': 'tool_use', 'tool': event['tool']})}\n\n"

            elif event["type"] == "result":
                cost_usd = event.get("cost_usd", 0)
                duration_ms = event.get("duration_ms", 0)
                if not full_content and event.get("text"):
                    full_content = event["text"]

            elif event["type"] == "error":
                yield f"data: {json.dumps({'type': 'error', 'message': event['message']})}\n\n"

    except Exception as e:
        # H4 fix: catch errors during streaming iteration
        logger.error(f"Error during streaming: {e}")
        yield f"data: {json.dumps({'type': 'error', 'message': 'AI service encountered an error.'})}\n\n"

    finally:
        await release_cli_slot()
        # H5 fix: save message even on disconnect/error
        if full_content:
            try:
                clean_content = sanitize_output(full_content)
                _save_message(
                    conversation_id, ai_msg_id, "assistant", clean_content, user_ctx,
                    cost_usd=cost_usd, duration_ms=duration_ms, model=model,
                )
                # Log retrieval with message_id for traceability
                log_retrieval(request.message, kb_ids, route, prompt_version=prompt_version, message_id=ai_msg_id)
                # Update user profile and extract memories
                update_profile(user_ctx.user_id, request.message, user_ctx.source_system)
                extract_memories_from_response(user_ctx.user_id, request.message, clean_content, user_ctx.source_system, conversation_id)
            except Exception:
                logger.exception("Failed to save assistant message after stream")

    # Done event
    yield f"data: {json.dumps({'type': 'done', 'message_id': ai_msg_id, 'cost_usd': cost_usd, 'duration_ms': duration_ms})}\n\n"
