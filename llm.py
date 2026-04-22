"""LLM backend abstraction.

The service historically used Claude Code CLI. We now support Gemini API as
the default backend, while keeping Claude CLI as an optional legacy fallback.

The public surface matches the old `claude_cli` module enough for minimal
changes in service code:
- query(...) -> LLMResult
- stream(...) -> async iterator of events:
  - {"type": "content", "text": "..."}
  - {"type": "tool_use", "tool": "...", "input": {...}}
  - {"type": "result", "text": "...", "cost_usd": 0, "duration_ms": ...}
  - {"type": "error", "message": "..."}
- generate_message_id() / generate_conversation_id()
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import httpx

import claude_cli
from config import settings
from services.kpi_tools import GEMINI_FUNCTION_DECLARATIONS, run_tool


class LLMError(Exception):
    """Raised when the configured LLM backend fails."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


@dataclass
class LLMResult:
    text: str
    cost_usd: float = 0.0
    duration_ms: int = 0
    model: str = ""


def generate_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:12]}"


def generate_conversation_id() -> str:
    return f"conv_{uuid.uuid4().hex[:12]}"


def _backend() -> str:
    return (settings.LLM_BACKEND or "gemini").strip().lower()


def _gemini_headers() -> dict[str, str]:
    if not settings.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY is not configured")
    return {
        "Content-Type": "application/json",
        "x-goog-api-key": settings.GEMINI_API_KEY,
    }


def _gemini_url(method: str, *, sse: bool = False) -> str:
    base = settings.GEMINI_BASE_URL.rstrip("/")
    url = f"{base}/models/{settings.GEMINI_MODEL}:{method}"
    if sse:
        url += "?alt=sse"
    return url


def _gemini_generation_config() -> dict[str, Any]:
    return {
        "temperature": settings.GEMINI_TEMPERATURE,
        "maxOutputTokens": settings.GEMINI_MAX_OUTPUT_TOKENS,
    }


def _ensure_role(content: dict) -> dict:
    if "role" not in content:
        content = {**content, "role": "model"}
    return content


def _extract_text_parts(content: dict) -> str:
    parts = (content or {}).get("parts") or []
    texts: list[str] = []
    for p in parts:
        t = p.get("text")
        if isinstance(t, str) and t:
            texts.append(t)
    return "".join(texts)


def _extract_function_calls(content: dict) -> list[dict]:
    parts = (content or {}).get("parts") or []
    calls: list[dict] = []
    for p in parts:
        fc = p.get("functionCall") or p.get("function_call")
        if isinstance(fc, dict) and fc.get("name"):
            calls.append(fc)
    return calls


async def _gemini_generate_content(payload: dict) -> dict:
    url = _gemini_url("generateContent")
    timeout = settings.GEMINI_TIMEOUT_SECONDS
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=_gemini_headers(), json=payload)
        if resp.status_code >= 400:
            raise LLMError("Gemini API error", detail=resp.text[:300])
        return resp.json()


async def _gemini_stream_content(payload: dict) -> AsyncIterator[str]:
    url = _gemini_url("streamGenerateContent", sse=True)
    timeout = settings.GEMINI_TIMEOUT_SECONDS
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=_gemini_headers(), json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise LLMError("Gemini API stream error", detail=body.decode("utf-8", errors="replace")[:300])

            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                # Gemini may send "[DONE]" in some environments.
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                candidates = chunk.get("candidates") or []
                if not candidates:
                    continue
                content = candidates[0].get("content") or {}
                text = _extract_text_parts(content)
                if text:
                    yield text


async def _gemini_resolve_tools(
    prompt: str,
    system_prompt: str | None,
    user_store_ids: list[int] | None,
) -> tuple[list[dict], list[dict]]:
    """Run Gemini tool-calling loop (non-stream), return (contents, tool_events)."""
    contents: list[dict] = [{"role": "user", "parts": [{"text": prompt}]}]
    tool_events: list[dict] = []

    tools = [{"functionDeclarations": GEMINI_FUNCTION_DECLARATIONS}]
    tool_cfg = {"functionCallingConfig": {"mode": settings.GEMINI_FUNCTION_CALLING_MODE}}

    # Safety guard to avoid infinite tool loops.
    for _ in range(8):
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": _gemini_generation_config(),
        }
        if system_prompt:
            payload["system_instruction"] = {"parts": [{"text": system_prompt}]}
        payload["tools"] = tools
        payload["toolConfig"] = tool_cfg

        data = await _gemini_generate_content(payload)
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("Gemini returned no candidates")

        model_content = _ensure_role(candidates[0].get("content") or {})
        calls = _extract_function_calls(model_content)
        if not calls:
            # No tool calls; keep model content in the transcript for final generation.
            contents.append(model_content)
            return contents, tool_events

        # Append the model tool call(s) content as-is (preserves id/thoughtSignature).
        contents.append(model_content)

        for call in calls:
            name = call.get("name")
            args = call.get("args") or {}
            tool_events.append({"type": "tool_use", "tool": name, "input": args})

            result = await run_tool(name, args, user_store_ids)
            fr: dict[str, Any] = {
                "name": name,
                # Wrap in {result: ...} per Gemini docs.
                "response": {"result": result},
            }
            # Gemini 3: id + thoughtSignature must be returned unchanged.
            if "id" in call:
                fr["id"] = call["id"]
            if "thoughtSignature" in call:
                fr["thoughtSignature"] = call["thoughtSignature"]
            if "thought_signature" in call:
                fr["thought_signature"] = call["thought_signature"]

            contents.append({"role": "user", "parts": [{"functionResponse": fr}]})

    raise LLMError("Gemini tool-calling exceeded max steps")


async def query(
    prompt: str,
    system_prompt: str = None,
    model: str = None,
    user_store_ids: list[int] = None,
    allowed_tools: list[str] = None,
    max_budget: float = None,
    json_schema_path: str = None,
    extra_mcp_env: dict = None,
) -> LLMResult:
    """Synchronous completion (no SSE), returns a full result."""
    start = time.perf_counter()

    b = _backend()
    if b in ("claude", "claude_cli"):
        try:
            r = await claude_cli.query(
                prompt=prompt,
                system_prompt=system_prompt,
                model=model,
                user_store_ids=user_store_ids,
                allowed_tools=allowed_tools,
                max_budget=max_budget,
                json_schema_path=json_schema_path,
                extra_mcp_env=extra_mcp_env,
            )
            return LLMResult(
                text=r.result,
                cost_usd=r.cost_usd,
                duration_ms=r.duration_ms,
                model=r.model or settings.CLAUDE_MODEL,
            )
        except claude_cli.CLIError as e:
            raise LLMError(str(e), detail=e.stderr) from e

    # Gemini
    contents, _tool_events = await _gemini_resolve_tools(prompt, system_prompt, user_store_ids)

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": _gemini_generation_config(),
    }
    if system_prompt:
        payload["system_instruction"] = {"parts": [{"text": system_prompt}]}

    data = await _gemini_generate_content(payload)
    candidates = data.get("candidates") or []
    if not candidates:
        raise LLMError("Gemini returned no candidates")
    text = _extract_text_parts(candidates[0].get("content") or {})
    dur_ms = int((time.perf_counter() - start) * 1000)
    return LLMResult(text=text, cost_usd=0.0, duration_ms=dur_ms, model=settings.GEMINI_MODEL)


async def stream(
    prompt: str,
    system_prompt: str = None,
    model: str = None,
    user_store_ids: list[int] = None,
    allowed_tools: list[str] = None,
    max_budget: float = None,
    extra_mcp_env: dict = None,
) -> AsyncIterator[dict]:
    """Streaming completion as event dicts consumed by chat_service."""
    start = time.perf_counter()

    b = _backend()
    if b in ("claude", "claude_cli"):
        try:
            async for ev in claude_cli.stream(
                prompt=prompt,
                system_prompt=system_prompt,
                model=model,
                user_store_ids=user_store_ids,
                allowed_tools=allowed_tools,
                max_budget=max_budget,
                extra_mcp_env=extra_mcp_env,
            ):
                yield ev
            return
        except claude_cli.CLIError as e:
            yield {"type": "error", "message": str(e)}
            return

    # Gemini: resolve tool calls (non-stream) then stream final answer.
    try:
        contents, tool_events = await _gemini_resolve_tools(prompt, system_prompt, user_store_ids)
    except Exception as e:
        yield {"type": "error", "message": str(e)}
        return

    for te in tool_events:
        yield te

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": _gemini_generation_config(),
    }
    if system_prompt:
        payload["system_instruction"] = {"parts": [{"text": system_prompt}]}

    full = ""
    try:
        async for chunk in _gemini_stream_content(payload):
            full += chunk
            yield {"type": "content", "text": chunk}
    except LLMError as e:
        yield {"type": "error", "message": str(e)}
        return

    dur_ms = int((time.perf_counter() - start) * 1000)
    yield {
        "type": "result",
        "text": full,
        "cost_usd": 0.0,
        "duration_ms": dur_ms,
    }

