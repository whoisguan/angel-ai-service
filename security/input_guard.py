"""Input sanitization — prompt injection detection.

Checks user input BEFORE sending to Claude CLI.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from db.sqlite_db import get_db

logger = logging.getLogger(__name__)

# Known prompt injection patterns (multilingual)
INJECTION_PATTERNS = [
    # English
    r"ignore\s+(previous|all|above)\s+(instructions|prompts|rules)",
    r"you\s+are\s+now\s+",
    r"new\s+instructions?\s*:",
    r"system\s*:\s*",
    r"ADMIN\s+OVERRIDE",
    r"reveal\s+(your|the)\s+(system|instructions|prompt)",
    r"print\s+your\s+(system|instructions|prompt)",
    # Italian
    r"ignora\s+(le\s+)?istruzioni\s+precedenti",
    r"ora\s+sei\s+",
    r"nuove\s+istruzioni\s*:",
    # Chinese
    r"忽略(之前|以上|所有)(的)?(指令|提示|规则)",
    r"你现在是",
    r"新(的)?指令\s*[:：]",
    # Structural
    r"<\s*/?\s*system\s*>",
    r"\\n\\n(Human|Assistant|System)\s*:",
]

_compiled = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def check_input(text: str, user_id: int = None, source_system: str = None) -> Optional[str]:
    """Check user input for prompt injection patterns.

    Returns None if safe, or the matched pattern name if suspicious.
    Logs rejection to rejected_queries table for observability.
    """
    for i, pattern in enumerate(_compiled):
        if pattern.search(text):
            logger.warning(f"Prompt injection detected: pattern #{i} matched in input")
            matched = INJECTION_PATTERNS[i]
            _log_rejection(user_id, source_system, "injection", text[:100], matched)
            return matched
    return None


def _log_rejection(user_id: int = None, source_system: str = None, reason: str = "injection",
                   query_preview: str = None, details: str = None):
    """Log a rejected query for observability."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            db.execute(
                """INSERT INTO rejected_queries (user_id, source_system, reason, query_preview, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, source_system, reason, query_preview, details, now),
            )
    except Exception:
        pass  # logging failure should never block the request


def sanitize_page_context(page_context: dict) -> dict:
    """Sanitize page_context to prevent injection via context fields.

    - Whitelist allowed keys
    - Limit value lengths
    - Strip potential injection content from values
    """
    ALLOWED_KEYS = {
        "page", "path", "store_id", "store_code", "store_name",
        "employee_id", "year", "month", "quarter", "period",
        "department_code", "user_role", "lang",
    }
    MAX_VALUE_LENGTH = 200

    clean = {}
    for key, value in page_context.items():
        if key not in ALLOWED_KEYS:
            continue
        value_str = str(value)[:MAX_VALUE_LENGTH]
        # Strip any newlines or special characters that could break prompt structure
        value_str = re.sub(r"[\n\r]", " ", value_str)
        clean[key] = value_str

    return clean
