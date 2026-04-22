"""AI Service configuration — loaded from environment variables."""

import os
from pathlib import Path

from pydantic_settings import BaseSettings

# Project root (directory containing this file)
_PROJECT_ROOT = Path(__file__).parent

class Settings(BaseSettings):
    # Service identity
    SERVICE_NAME: str = "angel-ai-service"
    SERVICE_PORT: int = 8001
    DEBUG: bool = False

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://192.168.1.110:5173"]

    # Authentication — service token for system-to-system calls
    SERVICE_TOKEN_SECRET: str = "change-me-in-production"

    # LLM backend
    # - "gemini": Google Gemini API
    # - "claude_cli": legacy Claude Code CLI backend
    # Default keeps production behavior until Gemini credentials are configured.
    LLM_BACKEND: str = "claude_cli"

    # Claude CLI settings
    CLAUDE_CLI_PATH: str = "claude"  # assumes claude is in PATH
    CLAUDE_MODEL: str = "sonnet"  # default model; can be "opus", "haiku", "sonnet"
    CLAUDE_MAX_BUDGET_USD: float = 0.50  # per-request safety cap
    CLAUDE_PERMISSION_MODE: str = "bypassPermissions"  # for automation
    CLAUDE_TIMEOUT_SECONDS: int = 120

    # Gemini API (Developer API via API key)
    GEMINI_API_KEY: str = ""  # set in .env; required when LLM_BACKEND="gemini"
    GEMINI_MODEL: str = "gemini-3-flash-preview"
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"
    GEMINI_TIMEOUT_SECONDS: int = 120
    GEMINI_TEMPERATURE: float = 1.0
    GEMINI_MAX_OUTPUT_TOKENS: int = 4096
    GEMINI_FUNCTION_CALLING_MODE: str = "AUTO"  # AUTO | ANY | NONE

    # MCP Server
    MCP_SERVER_SCRIPT: str = str(_PROJECT_ROOT / "mcp_server.py")
    MCP_PYTHON_PATH: str = "python"

    # KPI Database (read-only, for MCP Server — ODBC connection string)
    KPI_DATABASE_URL: str = "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost\\SQLSERVER;DATABASE=angel_kpi;UID=sa;PWD=sa;TrustServerCertificate=yes;"
    BI_SQLSERVER_URL: str = ""  # optional: mssql+pyodbc://...

    # Rate limiting
    MAX_REQUESTS_PER_USER_PER_DAY: int = 100
    MAX_CONCURRENT_REQUESTS: int = 3

    # Timezone for daily limit reset (Europe/Rome for Italian workplace)
    DAILY_LIMIT_TIMEZONE: str = "Europe/Rome"

    # SQLite for conversation history & usage tracking
    SQLITE_DB_PATH: str = str(_PROJECT_ROOT / "data" / "ai_service.db")

    # System prompt
    SYSTEM_PROMPT_PATH: str = str(_PROJECT_ROOT / "prompts" / "system_prompt.txt")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
