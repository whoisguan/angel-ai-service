"""Knowledge service — FTS5 search, question routing, and structured context injection."""

import json
import logging
import re
from datetime import datetime, timezone

from db.sqlite_db import get_db

logger = logging.getLogger(__name__)

# Patterns that indicate a dynamic (real-time data) question
DYNAMIC_PATTERNS = [
    r'\b(202[4-9]|20[3-9]\d)\b',           # year mention
    r'\b(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)\b',
    r'\b(january|february|march|april|may|june|july|august|september|october|november|december)\b',
    r'\b\d+月\b',                            # Chinese month
    r'\bNEG\.?\d+\b',                        # store code
    r'\b(negozio|store|门店|店)\s*\d+\b',
    r'€\s*[\d.,]+',                          # money amount
    r'\b(fatturato|revenue|营收|收入|bonus|奖金|surplus|盈余)\b',
    r'\b(quanto|how much|多少|几)\b',
    r'\b(classifica|ranking|排名)\b',
]

# Patterns that indicate a static (knowledge) question
STATIC_PATTERNS = [
    r'\b(cos[\'ìe]|what is|什么是|啥是)\b',
    r'\b(come si calcola|how.*calculated|如何计算|怎么算)\b',
    r'\b(regola|rule|规则|规定)\b',
    r'\b(procedura|process|流程|步骤)\b',
    r'\b(significato|meaning|含义|意思)\b',
    r'\b(spiegami|explain|解释|说明)\b',
    r'\b(definizione|definition|定义)\b',
]

DYNAMIC_RE = [re.compile(p, re.IGNORECASE) for p in DYNAMIC_PATTERNS]
STATIC_RE = [re.compile(p, re.IGNORECASE) for p in STATIC_PATTERNS]


def route_question(query: str) -> str:
    """Classify a question as 'static', 'dynamic', or 'hybrid'.

    static  → glossary/rule/process explanation, served from knowledge base
    dynamic → requires real-time data from MCP/database
    hybrid  → needs both knowledge context AND live data
    """
    dynamic_score = sum(1 for p in DYNAMIC_RE if p.search(query))
    static_score = sum(1 for p in STATIC_RE if p.search(query))

    if static_score > 0 and dynamic_score > 0:
        return "hybrid"
    if static_score > 0:
        return "static"
    if dynamic_score > 0:
        return "dynamic"
    # Default: treat as dynamic (safer — will use MCP)
    return "dynamic"


def search_knowledge(query: str, limit: int = 3) -> list[dict]:
    """Search the knowledge base using FTS5. Returns only verified entries."""
    try:
        with get_db() as db:
            # Check if FTS table has content
            count = db.execute("SELECT COUNT(*) FROM knowledge_base WHERE status = 'verified'").fetchone()[0]
            if count == 0:
                return []

            # Escape FTS5 operators by quoting the query as a phrase
            safe_query = '"' + query.replace('"', '""') + '"'
            rows = db.execute(
                """SELECT kb.id, kb.question, kb.answer, kb.category, kb.tags,
                          kb.confidence, kb.scope,
                          rank AS fts_rank
                   FROM kb_fts
                   JOIN knowledge_base kb ON kb.id = kb_fts.rowid
                   WHERE kb_fts MATCH ?
                     AND kb.status = 'verified'
                   ORDER BY rank
                   LIMIT ?""",
                (safe_query, limit),
            ).fetchall()

            return [
                {
                    "id": r["id"],
                    "question": r["question"],
                    "answer": r["answer"],
                    "category": r["category"],
                    "tags": r["tags"],
                    "confidence": r["confidence"],
                    "score": round(-r["fts_rank"], 2),  # FTS5 rank is negative; invert for readability
                }
                for r in rows
            ]
    except Exception as e:
        logger.warning(f"Knowledge search failed: {e}")
        return []


def build_knowledge_context(query: str) -> str | None:
    """Build structured knowledge context for injection into the prompt.

    Returns None if no relevant knowledge found or question is purely dynamic.
    """
    route = route_question(query)

    if route == "dynamic":
        return None

    results = search_knowledge(query)
    if not results:
        return None

    lines = [
        "\n## Retrieved Knowledge (reference only — do NOT use cached numbers, always query live data)",
    ]
    for i, r in enumerate(results, 1):
        lines.append(
            f"[{i}] (score:{r['score']}, category:{r['category']}) "
            f"Q: {r['question']}\n    A: {r['answer']}"
        )

    return "\n".join(lines)


def log_retrieval(
    query: str,
    kb_ids: list[int],
    route: str,
    feedback: str = None,
    prompt_version: str = None,
    mcp_tools: list[str] = None,
):
    """Log a retrieval event for the feedback loop."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            db.execute(
                """INSERT INTO retrieval_feedback
                   (query, retrieved_kb_ids, route_decision, user_feedback, prompt_version, mcp_tools_used, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    query,
                    json.dumps(kb_ids) if kb_ids else None,
                    route,
                    feedback,
                    prompt_version,
                    json.dumps(mcp_tools) if mcp_tools else None,
                    now,
                ),
            )
    except Exception as e:
        logger.warning(f"Failed to log retrieval: {e}")
