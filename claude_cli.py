"""Claude Code CLI wrapper — the core LLM integration layer.

Calls `claude -p` in headless mode, using Max subscription.
Supports both synchronous JSON and streaming NDJSON output.

Windows compatibility: uses subprocess.Popen + threading instead of
asyncio.create_subprocess_exec, which raises NotImplementedError on
Windows uvicorn's SelectorEventLoop.
"""

import asyncio
import json
import os
import platform
import queue
import subprocess
import tempfile
import threading
import uuid
from typing import AsyncIterator, Optional

from config import settings

# On Windows, .cmd/.bat files need shell=True for subprocess.run/Popen
_IS_WINDOWS = platform.system() == "Windows"

def _get_env():
    """Build subprocess env, ensuring node/npm/python are in PATH on Windows."""
    env = os.environ.copy()
    if _IS_WINDOWS:
        extra = r"C:\Program Files\nodejs;C:\Users\SQLTS\AppData\Roaming\npm;C:\Users\SQLTS\AppData\Local\Programs\Python\Python311"
        env["PATH"] = extra + ";" + env.get("PATH", "")
    return env


class CLIError(Exception):
    """Raised when claude CLI returns an error."""

    def __init__(self, message: str, stderr: str = "", exit_code: int = -1):
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code


class CLIResult:
    """Parsed result from a claude -p JSON call."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.result = raw.get("result", "")
        self.is_error = raw.get("is_error", False)
        self.cost_usd = raw.get("total_cost_usd", 0.0)
        self.duration_ms = raw.get("duration_ms", 0)
        self.session_id = raw.get("session_id", "")
        self.model = ""
        # Extract model from modelUsage
        model_usage = raw.get("modelUsage", {})
        if model_usage:
            self.model = next(iter(model_usage.keys()), "")


def _build_mcp_config(user_store_ids: list[int], extra_env: dict = None) -> dict:
    """Build MCP config JSON with user permissions injected via env vars."""
    env = {
        "KPI_DATABASE_URL": settings.KPI_DATABASE_URL,
        "USER_STORE_IDS": "*" if user_store_ids is None else ",".join(str(s) for s in user_store_ids),
    }
    if settings.BI_SQLSERVER_URL:
        env["BI_SQLSERVER_URL"] = settings.BI_SQLSERVER_URL
    if extra_env:
        env.update(extra_env)

    return {
        "mcpServers": {
            "kpi-data": {
                "command": settings.MCP_PYTHON_PATH,
                "args": [settings.MCP_SERVER_SCRIPT],
                "env": env,
            }
        }
    }


def _build_command(
    prompt: str,
    system_prompt: str = None,
    model: str = None,
    mcp_config_path: str = None,
    output_format: str = "json",
    allowed_tools: list[str] = None,
    max_budget: float = None,
    json_schema_path: str = None,
) -> list[str]:
    """Build the claude CLI command arguments."""
    cmd = [
        settings.CLAUDE_CLI_PATH,
        "-p", prompt,
        "--output-format", output_format,
        "--no-session-persistence",
        "--permission-mode", settings.CLAUDE_PERMISSION_MODE,
    ]

    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    if model:
        cmd.extend(["--model", model])
    else:
        cmd.extend(["--model", settings.CLAUDE_MODEL])

    if mcp_config_path:
        cmd.extend(["--mcp-config", mcp_config_path])
        cmd.append("--strict-mcp-config")

    if allowed_tools:
        for tool in allowed_tools:
            cmd.extend(["--allowedTools", tool])

    budget = max_budget or settings.CLAUDE_MAX_BUDGET_USD
    cmd.extend(["--max-budget-usd", str(budget)])

    if json_schema_path:
        cmd.extend(["--json-schema", json_schema_path])

    if output_format == "stream-json":
        cmd.append("--verbose")

    return cmd


async def query(
    prompt: str,
    system_prompt: str = None,
    model: str = None,
    user_store_ids: list[int] = None,
    allowed_tools: list[str] = None,
    max_budget: float = None,
    json_schema_path: str = None,
    extra_mcp_env: dict = None,
) -> CLIResult:
    """Execute a synchronous claude -p call, return parsed result."""

    # Write MCP config to temp file
    mcp_config = _build_mcp_config(user_store_ids, extra_mcp_env)
    mcp_config_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(mcp_config, f)
            mcp_config_path = f.name

        cmd = _build_command(
            prompt=prompt,
            system_prompt=system_prompt,
            model=model,
            mcp_config_path=mcp_config_path,
            output_format="json",
            allowed_tools=allowed_tools,
            max_budget=max_budget,
            json_schema_path=json_schema_path,
        )

        import logging
        _log = logging.getLogger(__name__)
        _env = _get_env()
        _log.info(f"CLI cmd[0]: {cmd[0]}, env PATH[0:300]: {_env.get('PATH','')[:300]}")

        def _run_cli():
            return subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.path.dirname(settings.MCP_SERVER_SCRIPT),
                timeout=settings.CLAUDE_TIMEOUT_SECONDS,
                shell=_IS_WINDOWS,
                env=_get_env(),
            )

        try:
            proc = await asyncio.to_thread(_run_cli)
        except subprocess.TimeoutExpired:
            raise CLIError(
                f"Claude CLI timed out after {settings.CLAUDE_TIMEOUT_SECONDS}s",
                exit_code=-1,
            )

        if proc.returncode != 0:
            raise CLIError(
                f"Claude CLI exited with code {proc.returncode}",
                stderr=proc.stderr.decode("utf-8", errors="replace"),
                exit_code=proc.returncode,
            )

        raw = json.loads(proc.stdout.decode("utf-8"))
        result = CLIResult(raw)

        if result.is_error:
            raise CLIError(f"Claude returned error: {result.result}")

        return result

    finally:
        if mcp_config_path and os.path.exists(mcp_config_path):
            os.unlink(mcp_config_path)


async def stream(
    prompt: str,
    system_prompt: str = None,
    model: str = None,
    user_store_ids: list[int] = None,
    allowed_tools: list[str] = None,
    max_budget: float = None,
    extra_mcp_env: dict = None,
) -> AsyncIterator[dict]:
    """Execute a streaming claude -p call, yield NDJSON events.

    Yields dicts with keys:
    - {"type": "init", ...} — session initialization
    - {"type": "content", "text": "..."} — incremental text
    - {"type": "tool_use", "tool": "...", "status": "..."} — MCP tool call
    - {"type": "result", "text": "...", "cost_usd": ..., "duration_ms": ...} — final
    - {"type": "error", "message": "..."} — error
    """

    mcp_config = _build_mcp_config(user_store_ids)
    mcp_config_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(mcp_config, f)
            mcp_config_path = f.name

        cmd = _build_command(
            prompt=prompt,
            system_prompt=system_prompt,
            model=model,
            mcp_config_path=mcp_config_path,
            output_format="stream-json",
            allowed_tools=allowed_tools,
            max_budget=max_budget,
        )

        _SENTINEL = None
        line_q: queue.Queue = queue.Queue()

        def _reader():
            """Run subprocess in a thread, push stdout lines to queue."""
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.path.dirname(settings.MCP_SERVER_SCRIPT),
                shell=_IS_WINDOWS,
                env=_get_env(),
            )
            try:
                for raw_line in proc.stdout:
                    line_q.put(raw_line)
                line_q.put(_SENTINEL)
            except Exception as exc:
                line_q.put(exc)
            finally:
                proc.wait()

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        try:
            while True:
                try:
                    item = await asyncio.to_thread(
                        line_q.get, timeout=settings.CLAUDE_TIMEOUT_SECONDS,
                    )
                except Exception:
                    yield {"type": "error", "message": "Stream timeout"}
                    return

                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    yield {"type": "error", "message": str(item)}
                    return

                line_str = item.decode("utf-8").strip()
                if not line_str:
                    continue

                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type == "system" and event.get("subtype") == "init":
                    yield {"type": "init", "session_id": event.get("session_id", "")}

                elif event_type == "assistant":
                    message = event.get("message", {})
                    for block in message.get("content", []):
                        if block.get("type") == "text":
                            yield {"type": "content", "text": block["text"]}
                        elif block.get("type") == "tool_use":
                            yield {
                                "type": "tool_use",
                                "tool": block.get("name", ""),
                                "input": block.get("input", {}),
                            }

                elif event_type == "result":
                    yield {
                        "type": "result",
                        "text": event.get("result", ""),
                        "cost_usd": event.get("total_cost_usd", 0),
                        "duration_ms": event.get("duration_ms", 0),
                    }

        finally:
            reader_thread.join(timeout=5)

    finally:
        if mcp_config_path and os.path.exists(mcp_config_path):
            os.unlink(mcp_config_path)


def generate_message_id() -> str:
    """Generate a unique message ID."""
    return f"msg_{uuid.uuid4().hex[:12]}"


def generate_conversation_id() -> str:
    """Generate a unique conversation ID."""
    return f"conv_{uuid.uuid4().hex[:12]}"
