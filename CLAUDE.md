# Angel AI Service — Claude Code Instructions

## Project Overview
Enterprise AI service layer for Angel Mercatone Due SRL. First consumer: angel-kpi system.
Uses Claude Code CLI (`claude -p`) with Max subscription as LLM backend.
MCP Server provides KPI database access tools.

## Tech Stack
| Component | Technology |
|-----------|-----------|
| Framework | FastAPI (Python 3.11+) |
| LLM | Claude Code CLI (Max subscription) |
| Data Tools | MCP Server (stdio transport) |
| Database | SQLite (history/usage), MySQL (KPI data, read-only) |
| Auth | Service Token (system-to-system) |

## Iron Rules
1. **AI Service is read-only** — never write to KPI or BI databases
2. **Permission enforcement in MCP** — every tool checks USER_STORE_IDS from env
3. **Output sanitization** — every response passes through PII detection before returning
4. **No API keys in code** — all secrets in .env
5. **CLI calls must have timeout** — never let a subprocess hang forever
6. **Test before deploy** — mock CLI calls in tests, don't consume Max quota in CI

## File Structure
- `main.py` — FastAPI entry point
- `claude_cli.py` — CLI wrapper (subprocess management, stream parsing)
- `mcp_server.py` — MCP Server (KPI data tools, stdio JSON-RPC)
- `routers/` — API endpoints
- `security/` — Auth + sanitization
- `services/` — Business logic
- `prompts/` — System prompt and JSON schemas

## Coding Rules
- Code and comments: English
- Python: PEP8, type hints, async where applicable
- No `print()` — use logging
- All database queries parameterized (no SQL injection)
