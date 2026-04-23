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
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx

_dbg = logging.getLogger("gemini_dbg")
logger = logging.getLogger(__name__)

import claude_cli
from config import settings
from services import key_store, role_router
from services.kpi_tools import GEMINI_FUNCTION_DECLARATIONS, run_tool


class LLMError(Exception):
    """Raised when the configured LLM backend fails.

    status_code / error_status / retry_after are populated for Gemini
    HTTP errors so the rotation wrapper can make structured decisions
    (instead of string-matching the body).
    """

    def __init__(
        self,
        message: str,
        detail: str = "",
        status_code: Optional[int] = None,
        error_status: Optional[str] = None,
        retry_after: Optional[int] = None,
    ):
        super().__init__(message)
        self.detail = detail
        self.status_code = status_code
        self.error_status = error_status
        self.retry_after = retry_after


def _is_quota_exhausted(err: LLMError) -> bool:
    return err.status_code == 429 or err.error_status == "RESOURCE_EXHAUSTED"


def _parse_gemini_error(
    status_code: int,
    body_text: str,
    headers: dict,
    message: str = "Gemini API error",
) -> LLMError:
    """Extract structured Gemini error fields from the response."""
    error_status = None
    retry_after = None
    try:
        body = json.loads(body_text) if body_text else {}
        err = body.get("error") or {}
        error_status = err.get("status")
        for d in err.get("details") or []:
            rd = d.get("retryDelay")
            if isinstance(rd, str) and rd.endswith("s"):
                try:
                    retry_after = int(float(rd[:-1]))
                    break
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    if retry_after is None:
        hdr = headers.get("retry-after") or headers.get("Retry-After")
        if hdr:
            try:
                retry_after = int(hdr)
            except (TypeError, ValueError):
                pass
    return LLMError(
        message,
        detail=(body_text or "")[:300],
        status_code=status_code,
        error_status=error_status,
        retry_after=retry_after,
    )


def _cooldown_until_iso(err: LLMError) -> Optional[str]:
    """Return ISO-8601 UTC timestamp for cooldown expiry. Uses the
    server-hinted retry_after if it's under 1h; otherwise None (caller
    falls back to next UTC midnight for typical daily-quota semantics).
    """
    if err.retry_after and 0 < err.retry_after < 3600:
        return (
            datetime.now(timezone.utc) + timedelta(seconds=err.retry_after)
        ).replace(microsecond=0).isoformat()
    return None


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


def _gemini_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        raise LLMError("Gemini API key is empty")
    return {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }


def _gemini_url(method: str, model: str, *, sse: bool = False) -> str:
    base = settings.GEMINI_BASE_URL.rstrip("/")
    url = f"{base}/models/{model}:{method}"
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


async def _gemini_generate_content(payload: dict, api_key: str, model: str) -> dict:
    url = _gemini_url("generateContent", model)
    timeout = settings.GEMINI_TIMEOUT_SECONDS
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=_gemini_headers(api_key), json=payload)
        if resp.status_code >= 400:
            raise _parse_gemini_error(resp.status_code, resp.text, dict(resp.headers))
        return resp.json()


async def _gemini_stream_content(
    payload: dict, api_key: str, model: str
) -> AsyncIterator[str]:
    url = _gemini_url("streamGenerateContent", model, sse=True)
    timeout = settings.GEMINI_TIMEOUT_SECONDS
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=_gemini_headers(api_key), json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise _parse_gemini_error(
                    resp.status_code,
                    body.decode("utf-8", errors="replace"),
                    dict(resp.headers),
                    message="Gemini API stream error",
                )

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
    api_key: str,
    model: str,
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

        data = await _gemini_generate_content(payload, api_key, model)
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("Gemini returned no candidates")

        model_content = _ensure_role(candidates[0].get("content") or {})
        calls = _extract_function_calls(model_content)
        _dbg.info("GEMINI_RAW model_content=%s calls=%s", str(model_content)[:800], calls)
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


async def _call_with_rotation(
    coro_factory: Callable[[str, str], Awaitable[Any]],
    role_names: list[str],
) -> Any:
    """Walk the key pool once per key, cooling down quota-exhausted keys.

    Falls back to settings.GEMINI_API_KEY when the pool is empty, which
    preserves legacy behavior until the admin UI is populated.
    """
    resolved_model = role_router.get_model_for_roles(role_names)
    tried: set[int] = set()
    last_error: Optional[LLMError] = None
    pool_had_candidates = False

    while True:
        picked = key_store.get_active_key(exclude_ids=tried)
        if picked is None:
            break
        pool_had_candidates = True
        key_id, api_key = picked
        tried.add(key_id)
        try:
            return await coro_factory(api_key, resolved_model)
        except LLMError as e:
            last_error = e
            if _is_quota_exhausted(e):
                key_store.mark_cooldown(key_id, _cooldown_until_iso(e))
                logger.warning(
                    "gemini key id=%s quota-exhausted; cooled down until=%s; trying next",
                    key_id, _cooldown_until_iso(e) or "next-utc-midnight",
                )
                continue
            raise

    # Pool had no candidates at all (empty or everything already cooling
    # down). Fall back to the legacy env key if configured — preserves
    # the pre-rotation behavior for deployments that haven't populated
    # the admin UI yet.
    if not pool_had_candidates and settings.GEMINI_API_KEY:
        logger.info("gemini key pool empty; using settings.GEMINI_API_KEY fallback")
        return await coro_factory(settings.GEMINI_API_KEY, resolved_model)

    if last_error and _is_quota_exhausted(last_error):
        raise LLMError(
            "all Gemini keys in pool are quota-exhausted",
            status_code=429,
            error_status=last_error.error_status,
        )
    raise LLMError("no usable Gemini API key (pool empty and no fallback)")


async def query(
    prompt: str,
    system_prompt: str = None,
    model: str = None,
    user_store_ids: list[int] = None,
    user_role_names: list[str] = None,
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

    role_names = user_role_names or []

    async def _do(api_key: str, resolved_model: str) -> LLMResult:
        contents, _ = await _gemini_resolve_tools(
            prompt, system_prompt, user_store_ids, api_key, resolved_model
        )
        # Short-circuit: when the tool-calling loop already produced text in
        # its final turn, return it directly. Re-prompting Gemini with a
        # transcript that ends in its own answer tends to yield empty.
        if contents and contents[-1].get("role") == "model":
            direct = _extract_text_parts(contents[-1])
            _dbg.info(
                "SHORT_CIRCUIT last_content=%s direct_len=%s",
                str(contents[-1])[:400], len(direct),
            )
            if direct:
                return LLMResult(
                    text=direct, cost_usd=0.0,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    model=resolved_model,
                )
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": _gemini_generation_config(),
        }
        if system_prompt:
            payload["system_instruction"] = {"parts": [{"text": system_prompt}]}
        data = await _gemini_generate_content(payload, api_key, resolved_model)
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("Gemini returned no candidates")
        text = _extract_text_parts(candidates[0].get("content") or {})
        return LLMResult(
            text=text, cost_usd=0.0,
            duration_ms=int((time.perf_counter() - start) * 1000),
            model=resolved_model,
        )

    return await _call_with_rotation(_do, role_names)


async def stream(
    prompt: str,
    system_prompt: str = None,
    model: str = None,
    user_store_ids: list[int] = None,
    user_role_names: list[str] = None,
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

    # Gemini with key-pool rotation on resolve_tools; stream_content
    # commits to the key chosen above (can't rotate mid-SSE).
    role_names = user_role_names or []
    resolved_model = role_router.get_model_for_roles(role_names)

    tried: set[int] = set()
    resolve_result: Optional[tuple[list[dict], list[dict], str]] = None
    last_error: Optional[LLMError] = None
    pool_had_candidates = False

    while True:
        picked = key_store.get_active_key(exclude_ids=tried)
        if picked is None:
            break
        pool_had_candidates = True
        key_id, api_key = picked
        tried.add(key_id)
        try:
            contents, tool_events = await _gemini_resolve_tools(
                prompt, system_prompt, user_store_ids, api_key, resolved_model
            )
            resolve_result = (contents, tool_events, api_key)
            break
        except LLMError as e:
            last_error = e
            if _is_quota_exhausted(e):
                key_store.mark_cooldown(key_id, _cooldown_until_iso(e))
                logger.warning(
                    "gemini key id=%s quota-exhausted (resolve); trying next",
                    key_id,
                )
                continue
            yield {"type": "error", "message": str(e)}
            return

    if resolve_result is None:
        if not pool_had_candidates and settings.GEMINI_API_KEY:
            try:
                contents, tool_events = await _gemini_resolve_tools(
                    prompt, system_prompt, user_store_ids,
                    settings.GEMINI_API_KEY, resolved_model,
                )
                resolve_result = (contents, tool_events, settings.GEMINI_API_KEY)
            except LLMError as e:
                yield {"type": "error", "message": str(e)}
                return
        else:
            msg = (
                "all Gemini keys in pool are quota-exhausted"
                if last_error and _is_quota_exhausted(last_error)
                else "no usable Gemini API key (pool empty and no fallback)"
            )
            yield {"type": "error", "message": msg}
            return

    contents, tool_events, committed_key = resolve_result
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
        async for chunk in _gemini_stream_content(payload, committed_key, resolved_model):
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

