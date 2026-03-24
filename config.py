"""AI Service configuration — loaded from environment variables."""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Service identity
    SERVICE_NAME: str = "angel-ai-service"
    SERVICE_PORT: int = 8001
    DEBUG: bool = False

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://192.168.1.110:5173"]

    # Authentication — service token for system-to-system calls
    SERVICE_TOKEN_SECRET: str = "change-me-in-production"

    # Claude CLI settings
    CLAUDE_CLI_PATH: str = "claude"  # assumes claude is in PATH
    CLAUDE_MODEL: str = "sonnet"  # default model; can be "opus", "haiku", "sonnet"
    CLAUDE_FALLBACK_MODEL: Optional[str] = "haiku"
    CLAUDE_MAX_BUDGET_USD: float = 0.50  # per-request safety cap
    CLAUDE_PERMISSION_MODE: str = "bypassPermissions"  # for automation
    CLAUDE_TIMEOUT_SECONDS: int = 120

    # MCP Server
    MCP_SERVER_SCRIPT: str = "C:/Projects/angel-ai-service/mcp_server.py"
    MCP_PYTHON_PATH: str = "python"

    # KPI Database (read-only, for MCP Server)
    KPI_DATABASE_URL: str = "mysql+pymysql://readonly:password@localhost/angel_kpi"
    BI_SQLSERVER_URL: str = ""  # optional: mssql+pyodbc://...

    # Rate limiting
    MAX_REQUESTS_PER_USER_PER_DAY: int = 100
    MAX_CONCURRENT_REQUESTS: int = 3

    # SQLite for conversation history & usage tracking
    SQLITE_DB_PATH: str = "C:/Projects/angel-ai-service/data/ai_service.db"

    # System prompt
    SYSTEM_PROMPT_PATH: str = "C:/Projects/angel-ai-service/prompts/system_prompt.txt"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
