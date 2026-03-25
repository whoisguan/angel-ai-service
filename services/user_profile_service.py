"""User profile service — tracks question patterns and preferences per user."""

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone

from db.sqlite_db import get_db

logger = logging.getLogger(__name__)

# Topic detection patterns (mapped to human-readable labels)
TOPIC_PATTERNS = [
    (re.compile(r'\b(bonus|premio|奖金)\b', re.IGNORECASE), "bonus"),
    (re.compile(r'\b(kpi|score|punteggio|评分)\b', re.IGNORECASE), "kpi_scores"),
    (re.compile(r'\b(fatturato|revenue|ricavo|营收|收入)\b', re.IGNORECASE), "revenue"),
    (re.compile(r'\b(classifica|ranking|排名)\b', re.IGNORECASE), "ranking"),
    (re.compile(r'\b(trend|tendenza|趋势)\b', re.IGNORECASE), "trends"),
    (re.compile(r'\b(dipendente|employee|员工)\b', re.IGNORECASE), "employees"),
    (re.compile(r'\b(negozio|store|门店)\b', re.IGNORECASE), "stores"),
    (re.compile(r'\b(reparto|department|部门)\b', re.IGNORECASE), "departments"),
    (re.compile(r'\b(anomal|异常)\b', re.IGNORECASE), "anomalies"),
    (re.compile(r'\b(calcol|calculat|计算|怎么算)\b', re.IGNORECASE), "calculations"),
]

# Language detection (simple heuristic)
_LANG_PATTERNS = {
    "zh": re.compile(r'[\u4e00-\u9fff]{2,}'),
    "it": re.compile(r'\b(come|perché|quanto|negozio|dipendente|calcola|classifica)\b', re.IGNORECASE),
    "en": re.compile(r'\b(how|what|why|which|employee|calculate|ranking)\b', re.IGNORECASE),
}


def detect_topics(message: str) -> list[str]:
    """Detect question topics from message text."""
    return [label for pattern, label in TOPIC_PATTERNS if pattern.search(message)]


def detect_language(message: str) -> str:
    """Detect primary language of a message."""
    # Chinese takes priority (clear signal)
    if _LANG_PATTERNS["zh"].search(message):
        return "zh"
    # Count Italian vs English signals
    it_count = len(_LANG_PATTERNS["it"].findall(message))
    en_count = len(_LANG_PATTERNS["en"].findall(message))
    if it_count > en_count:
        return "it"
    if en_count > it_count:
        return "en"
    return "it"  # default for Italian workplace


def update_profile(user_id: int, message: str, source_system: str = "angel-kpi"):
    """Update user profile after a chat interaction.

    Lightweight: runs after each chat, designed to be fast (no heavy aggregation).
    """
    now = datetime.now(timezone.utc).isoformat()
    topics = detect_topics(message)
    lang = detect_language(message)

    try:
        with get_db() as db:
            existing = db.execute(
                "SELECT top_topics, question_count, preferred_language, created_at FROM user_profiles WHERE user_id = ? AND source_system = ?",
                (user_id, source_system),
            ).fetchone()

            if existing:
                existing = dict(existing)  # sqlite3.Row → dict for .get() support
                # Merge topic counts (stored as dict: {"bonus": 5, "kpi_scores": 3})
                old_topic_counts = json.loads(existing["top_topics"]) if existing["top_topics"] else {}
                if isinstance(old_topic_counts, list):
                    # Migration: convert old flat list format to dict
                    old_topic_counts = Counter(old_topic_counts)
                topic_counter = Counter(old_topic_counts)
                topic_counter.update(topics)
                # Keep top 10
                top_topics = dict(topic_counter.most_common(10))

                new_count = existing["question_count"] + 1

                # Calculate avg questions per day using created_at (first interaction)
                created_at = existing.get("created_at") or now
                try:
                    first_dt = datetime.fromisoformat(created_at)
                    days = max((datetime.now(timezone.utc) - first_dt).days, 1)
                    avg_per_day = round(new_count / days, 1)
                except (ValueError, TypeError):
                    avg_per_day = 0

                # Language: weighted toward recent (keep existing if same)
                final_lang = lang if lang != "it" else existing["preferred_language"] or "it"

                db.execute(
                    """UPDATE user_profiles
                       SET top_topics = ?, question_count = ?, avg_questions_per_day = ?,
                           preferred_language = ?, last_active = ?, updated_at = ?
                       WHERE user_id = ? AND source_system = ?""",
                    (json.dumps(top_topics), new_count, avg_per_day, final_lang, now, now, user_id, source_system),
                )
            else:
                initial_topics = dict(Counter(topics))
                db.execute(
                    """INSERT INTO user_profiles
                       (user_id, source_system, top_topics, question_count, avg_questions_per_day, preferred_language, created_at, last_active, updated_at)
                       VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?)""",
                    (user_id, source_system, json.dumps(initial_topics), lang, now, now, now),
                )
    except Exception as e:
        logger.warning(f"Failed to update user profile: {e}")


def get_profile_summary(user_id: int, source_system: str = "angel-kpi") -> str | None:
    """Get a concise profile summary for prompt injection.

    Returns None if no profile exists or too few interactions.
    Output is sanitized (no markup, length-capped) to prevent prompt injection.
    """
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT top_topics, question_count, preferred_language FROM user_profiles WHERE user_id = ? AND source_system = ?",
                (user_id, source_system),
            ).fetchone()

        if not row or row["question_count"] < 3:
            return None  # Too few interactions to build useful profile

        raw_topics = json.loads(row["top_topics"]) if row["top_topics"] else {}
        if isinstance(raw_topics, dict):
            # Dict format: {"bonus": 5, "kpi_scores": 3} — sorted by count
            top3 = [k for k, _ in sorted(raw_topics.items(), key=lambda x: -x[1])[:3]]
        else:
            # Legacy flat list format
            top3 = raw_topics[:3]

        if not top3:
            return None

        # Sanitize: only known safe topic labels, no user-generated content
        safe_topics = [t for t in top3 if t.isalpha() or t.replace("_", "").isalpha()]
        lang = row["preferred_language"] if row["preferred_language"] in ("it", "en", "zh") else "it"

        return (
            f"This user has asked {row['question_count']} questions. "
            f"Main interests: {', '.join(safe_topics)}. "
            f"Preferred language: {lang}."
        )
    except Exception as e:
        logger.debug(f"Failed to get profile summary: {e}")
        return None
