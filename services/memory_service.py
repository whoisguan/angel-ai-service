"""Cross-conversation memory service — persistent user context across chat sessions.

Memories are user-scoped facts extracted from conversations that improve
future responses. They have TTL-based expiration and support soft deletion.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from db.sqlite_db import get_db

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_TTL_DAYS = 30
MAX_MEMORIES_PER_USER = 20

# Patterns that indicate extractable user-specific context
# SECURITY: Only extract structured values (store numbers, whitelisted role labels).
# Free-text capture (like department name) is NOT allowed — it creates persistent prompt injection risk.
_MEMORY_EXTRACTION_PATTERNS = [
    # User references a specific store they manage (only captures store number — safe)
    (re.compile(r'(?:gestisco|manage|负责|管理)\s+(?:il\s+)?(?:negozio|store|门店)\s*(?:NEG\.?)?(\d{1,3})', re.IGNORECASE),
     "managed_store", "User manages store {0}"),
    # User references their role using whitelisted labels only
    (re.compile(r'(?:sono|I am|我是)\s+(?:il\s+)?(?:un\s+)?(responsabile|direttore|manager|店长|经理)', re.IGNORECASE),
     "user_role_stated", "User role: {0}"),
]


def save_memory(
    user_id: int,
    memory_key: str,
    content: str,
    source_system: str = "angel-kpi",
    source_conversation_id: str = None,
    ttl_days: int = DEFAULT_MEMORY_TTL_DAYS,
) -> bool:
    """Save or update a memory for a user. Returns True on success.

    If a memory with the same key exists (and is not deleted), it is updated.
    Content is sanitized (no markup, length-capped) to prevent prompt injection.
    """
    # Sanitize content: strip markup, cap length
    content = re.sub(r'[#*`\[\]<>{}]', '', content)[:500]

    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=ttl_days)).isoformat()

    try:
        with get_db() as db:
            # Check for existing active memory with same key
            existing = db.execute(
                "SELECT id FROM user_memories WHERE user_id = ? AND source_system = ? AND memory_key = ? AND is_deleted = 0",
                (user_id, source_system, memory_key),
            ).fetchone()

            if existing:
                db.execute(
                    """UPDATE user_memories
                       SET content = ?, source_conversation_id = ?, expires_at = ?
                       WHERE id = ?""",
                    (content, source_conversation_id, expires, existing["id"]),
                )
            else:
                # Check total memory count for user
                count = db.execute(
                    "SELECT COUNT(*) FROM user_memories WHERE user_id = ? AND source_system = ? AND is_deleted = 0",
                    (user_id, source_system),
                ).fetchone()[0]

                if count >= MAX_MEMORIES_PER_USER:
                    # Evict oldest memory
                    db.execute(
                        """DELETE FROM user_memories
                           WHERE id = (
                               SELECT id FROM user_memories
                               WHERE user_id = ? AND source_system = ? AND is_deleted = 0
                               ORDER BY created_at ASC LIMIT 1
                           )""",
                        (user_id, source_system),
                    )

                db.execute(
                    """INSERT INTO user_memories
                       (user_id, source_system, memory_key, content, source_conversation_id, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, source_system, memory_key, content, source_conversation_id, now.isoformat(), expires),
                )
        return True
    except Exception as e:
        logger.warning(f"Failed to save memory: {e}")
        return False


def get_memories(user_id: int, source_system: str = "angel-kpi", limit: int = 5) -> list[dict]:
    """Get active, non-expired memories for a user."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as db:
            rows = db.execute(
                """SELECT memory_key, content, created_at, expires_at
                   FROM user_memories
                   WHERE user_id = ? AND source_system = ? AND is_deleted = 0 AND expires_at > ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (user_id, source_system, now, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"Failed to get memories: {e}")
        return []


def delete_memory(user_id: int, memory_key: str, source_system: str = "angel-kpi") -> bool:
    """Soft-delete a memory by key."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as db:
            cursor = db.execute(
                """UPDATE user_memories
                   SET is_deleted = 1, deleted_at = ?
                   WHERE user_id = ? AND source_system = ? AND memory_key = ? AND is_deleted = 0""",
                (now, user_id, source_system, memory_key),
            )
        return cursor.rowcount > 0
    except Exception as e:
        logger.warning(f"Failed to delete memory: {e}")
        return False


def delete_all_memories(user_id: int, source_system: str = "angel-kpi") -> int:
    """Soft-delete all memories for a user. Returns count deleted."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as db:
            cursor = db.execute(
                "UPDATE user_memories SET is_deleted = 1, deleted_at = ? WHERE user_id = ? AND source_system = ? AND is_deleted = 0",
                (now, user_id, source_system),
            )
        return cursor.rowcount
    except Exception as e:
        logger.warning(f"Failed to delete all memories: {e}")
        return 0


def cleanup_expired():
    """Hard-delete memories past their expiration. Run periodically."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as db:
            cursor = db.execute(
                "DELETE FROM user_memories WHERE expires_at <= ? OR (is_deleted = 1 AND deleted_at <= ?)",
                (now, now),
            )
            if cursor.rowcount > 0:
                logger.info(f"Cleaned up {cursor.rowcount} expired/deleted memories")
    except Exception as e:
        logger.warning(f"Memory cleanup failed: {e}")


def build_memory_context(user_id: int, source_system: str = "angel-kpi") -> str | None:
    """Build memory context section for prompt injection.

    Returns a formatted string or None if no memories exist.
    """
    memories = get_memories(user_id, source_system=source_system, limit=5)
    if not memories:
        return None

    lines = ["\n## User Memory (from previous conversations)"]
    for m in memories:
        # Sanitize: strip any markup, cap length per entry
        key = re.sub(r'[^a-zA-Z0-9_]', '', m['memory_key'])[:50]
        content = re.sub(r'[#*`\[\]<>{}]', '', m['content'])[:200]
        lines.append(f"- {key}: {content}")

    return "\n".join(lines)


def extract_memories_from_response(
    user_id: int,
    user_message: str,
    ai_response: str,
    source_system: str = "angel-kpi",
    conversation_id: str = None,
):
    """Extract potential memories from a conversation exchange.

    Uses simple heuristics to detect user-specific context worth remembering.
    Only extracts from user messages (not AI responses) to avoid hallucinated facts.
    """
    for pattern, key, template in _MEMORY_EXTRACTION_PATTERNS:
        match = pattern.search(user_message)
        if match:
            content = template.format(*match.groups())
            save_memory(user_id, key, content, source_system=source_system, source_conversation_id=conversation_id)
            logger.debug(f"Extracted memory for user {user_id}: {key} = {content}")
