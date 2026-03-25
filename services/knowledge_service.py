"""Knowledge service — FTS5 search, question routing, structured context injection, and retrieval caching."""

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from db.sqlite_db import get_db

CACHE_TTL_DAYS = 7

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


def _build_scope_key(user_roles: list[str] = None) -> str:
    """Build a deterministic scope key from user roles."""
    accessible_scopes = _get_accessible_scopes(user_roles)
    return "|".join(sorted(accessible_scopes))


def _get_accessible_scopes(user_roles: list[str] = None) -> list[str]:
    """Determine accessible scopes based on user roles."""
    accessible_scopes = ["all"]
    if user_roles:
        if any(r in ("admin", "ROLE_SUPER_ADMIN", "ROLE_ADMIN") for r in user_roles):
            accessible_scopes.extend(["admin", "store_manager", "employee"])
        elif any(r in ("store_manager", "ROLE_STORE_MANAGER") for r in user_roles):
            accessible_scopes.extend(["store_manager", "employee"])
        else:
            accessible_scopes.append("employee")
    return accessible_scopes


def _make_cache_key(query: str, scope_key: str) -> str:
    """Create a deterministic cache key from query + scope."""
    normalized = query.lower().strip()
    raw = f"{normalized}|{scope_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _get_cached_results(cache_key: str, user_roles: list[str] = None) -> list[dict] | None:
    """Check retrieval cache. Returns cached KB results or None on miss.

    Re-validates scope on read to handle KB scope changes between cache write and read.
    Preserves original ranking order from kb_ids.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            row = db.execute(
                "SELECT kb_ids, kb_scores FROM retrieval_cache WHERE cache_key = ? AND expires_at > ?",
                (cache_key, now),
            ).fetchone()
            if not row:
                return None

            kb_ids = json.loads(row["kb_ids"])
            kb_scores = json.loads(row["kb_scores"])

            if not kb_ids:
                return []

            # Re-validate: fetch only entries still verified AND within current scope
            accessible_scopes = _get_accessible_scopes(user_roles)
            id_placeholders = ",".join(["?"] * len(kb_ids))
            scope_placeholders = ",".join(["?"] * len(accessible_scopes))
            rows = db.execute(
                f"""SELECT id, question, answer, category, tags, confidence
                    FROM knowledge_base
                    WHERE id IN ({id_placeholders})
                      AND status = 'verified'
                      AND scope IN ({scope_placeholders})""",
                (*kb_ids, *accessible_scopes),
            ).fetchall()

            # Rebuild results preserving original kb_ids order
            score_map = dict(zip(kb_ids, kb_scores))
            row_map = {r["id"]: r for r in rows}
            results = []
            for kid in kb_ids:
                r = row_map.get(kid)
                if r:
                    results.append({
                        "id": r["id"],
                        "question": r["question"],
                        "answer": r["answer"],
                        "category": r["category"],
                        "tags": r["tags"],
                        "confidence": r["confidence"],
                        "score": score_map.get(kid, 0),
                    })

            # Update hit count
            db.execute("UPDATE retrieval_cache SET hit_count = hit_count + 1 WHERE cache_key = ?", (cache_key,))

            return results
    except Exception as e:
        logger.debug(f"Cache lookup failed: {e}")
        return None


def _store_cache(cache_key: str, query: str, scope_key: str, results: list[dict]):
    """Store FTS5 retrieval results in cache."""
    try:
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(days=CACHE_TTL_DAYS)).isoformat()
        kb_ids = [r["id"] for r in results]
        kb_scores = [r["score"] for r in results]

        with get_db() as db:
            db.execute(
                """INSERT OR REPLACE INTO retrieval_cache
                   (cache_key, query, scope_key, kb_ids, kb_scores, hit_count, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                (cache_key, query, scope_key, json.dumps(kb_ids), json.dumps(kb_scores), now.isoformat(), expires),
            )
    except Exception as e:
        logger.debug(f"Cache store failed: {e}")


def invalidate_cache_for_kb(kb_id: int = None):
    """Invalidate cache entries. If kb_id given, only entries containing that id; otherwise all."""
    try:
        with get_db() as db:
            if kb_id is not None:
                # Use json_each for indexed single-query deletion (no full table scan)
                db.execute(
                    """DELETE FROM retrieval_cache
                       WHERE id IN (
                           SELECT rc.id FROM retrieval_cache rc, json_each(rc.kb_ids) je
                           WHERE je.value = ?
                       )""",
                    (kb_id,),
                )
            else:
                db.execute("DELETE FROM retrieval_cache")
    except Exception as e:
        logger.warning(f"Cache invalidation failed: {e}")


def cleanup_expired_cache():
    """Remove expired cache entries."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            db.execute("DELETE FROM retrieval_cache WHERE expires_at <= ?", (now,))
    except Exception as e:
        logger.debug(f"Cache cleanup failed: {e}")


def search_knowledge(query: str, user_roles: list[str] = None, limit: int = 3) -> list[dict]:
    """Search the knowledge base using FTS5 with caching. Returns only verified entries matching user scope."""
    scope_key = _build_scope_key(user_roles)
    cache_key = _make_cache_key(query, scope_key)

    # Check cache first (re-validates scope on read)
    cached = _get_cached_results(cache_key, user_roles=user_roles)
    if cached is not None:
        logger.debug(f"Cache hit for query: {query[:50]}...")
        return cached[:limit]

    try:
        with get_db() as db:
            # Check if FTS table has content
            count = db.execute("SELECT COUNT(*) FROM knowledge_base WHERE status = 'verified'").fetchone()[0]
            if count == 0:
                return []

            # Escape FTS5 operators by quoting the query as a phrase
            safe_query = '"' + query.replace('"', '""') + '"'

            accessible_scopes = _get_accessible_scopes(user_roles)
            placeholders = ",".join(["?"] * len(accessible_scopes))
            rows = db.execute(
                f"""SELECT kb.id, kb.question, kb.answer, kb.category, kb.tags,
                          kb.confidence, kb.scope,
                          rank AS fts_rank
                   FROM kb_fts
                   JOIN knowledge_base kb ON kb.id = kb_fts.rowid
                   WHERE kb_fts MATCH ?
                     AND kb.status = 'verified'
                     AND kb.scope IN ({placeholders})
                   ORDER BY rank
                   LIMIT ?""",
                (safe_query, *accessible_scopes, limit),
            ).fetchall()

            results = [
                {
                    "id": r["id"],
                    "question": r["question"],
                    "answer": r["answer"],
                    "category": r["category"],
                    "tags": r["tags"],
                    "confidence": r["confidence"],
                    "score": round(-r["fts_rank"], 2),
                }
                for r in rows
            ]

            # Cache results (only for static route queries with results)
            if results:
                _store_cache(cache_key, query, scope_key, results)

            return results
    except Exception as e:
        logger.warning(f"Knowledge search failed: {e}")
        return []


def build_knowledge_context(query: str, user_roles: list[str] = None) -> tuple[str | None, str, list[int]]:
    """Build structured knowledge context for injection into the prompt.

    Returns (context_str | None, route_decision, matched_kb_ids).
    """
    route = route_question(query)

    if route == "dynamic":
        return None, route, []

    results = search_knowledge(query, user_roles=user_roles)
    kb_ids = [r["id"] for r in results]
    if not results:
        return None, route, kb_ids

    lines = [
        "\n## Retrieved Knowledge (reference only — do NOT use cached numbers, always query live data)",
    ]
    for i, r in enumerate(results, 1):
        lines.append(
            f"[{i}] (score:{r['score']}, category:{r['category']}) "
            f"Q: {r['question']}\n    A: {r['answer']}"
        )

    return "\n".join(lines), route, kb_ids


def log_retrieval(
    query: str,
    kb_ids: list[int],
    route: str,
    feedback: str = None,
    prompt_version: str = None,
    mcp_tools: list[str] = None,
    message_id: str = None,
) -> int | None:
    """Log a retrieval event for the feedback loop. Returns the event id."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO retrieval_feedback
                   (query, retrieved_kb_ids, route_decision, user_feedback, prompt_version, mcp_tools_used, message_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    query,
                    json.dumps(kb_ids) if kb_ids else None,
                    route,
                    feedback,
                    prompt_version,
                    json.dumps(mcp_tools) if mcp_tools else None,
                    message_id,
                    now,
                ),
            )
            return cursor.lastrowid
    except Exception as e:
        logger.warning(f"Failed to log retrieval: {e}")
        return None
