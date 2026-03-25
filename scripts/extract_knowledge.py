"""Extract Q&A pairs from thumbs-up messages to seed the knowledge base.

Usage:
    cd angel-ai-service
    python scripts/extract_knowledge.py [--dry-run] [--limit 200]

Reads messages that received 'helpful' feedback, uses Claude CLI to extract
generalizable Q&A pairs, and inserts them as 'draft' entries in knowledge_base.
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone

# Add parent dir to path for imports
sys.path.insert(0, ".")
from db.sqlite_db import get_db, init_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

EXTRACT_PROMPT = """You are a knowledge extraction assistant. Given a chat exchange (user question + AI answer) from a KPI/bonus system, extract a generalizable Q&A pair.

Rules:
1. Remove specific numbers, dates, store names, employee names — make it general
2. If the answer contains time-specific data (e.g., "January 2026 revenue was €146,206"), DO NOT extract it
3. Only extract if the answer explains a concept, rule, process, or calculation method
4. Classify as: glossary (term definition), rule (business rule), faq (common question), process (how-to)

Return JSON (or null if not extractable):
{"question": "...", "answer": "...", "category": "glossary|rule|faq|process", "tags": "comma,separated,tags"}

Chat exchange:
User: {user_message}
Assistant: {ai_message}"""


def get_helpful_exchanges(limit: int = 200) -> list[dict]:
    """Get message pairs that received helpful feedback."""
    with get_db() as db:
        rows = db.execute(
            """SELECT f.message_id, m.conversation_id, m.content as ai_content, m.id as ai_msg_id
               FROM feedback f
               JOIN messages m ON f.message_id = m.id
               WHERE f.rating = 'helpful' AND m.role = 'assistant'
               ORDER BY f.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

        exchanges = []
        for r in rows:
            # Find the preceding user message
            user_msg = db.execute(
                """SELECT content FROM messages
                   WHERE conversation_id = ? AND role = 'user' AND created_at < (
                       SELECT created_at FROM messages WHERE id = ?
                   )
                   ORDER BY created_at DESC LIMIT 1""",
                (r["conversation_id"], r["ai_msg_id"]),
            ).fetchone()

            if user_msg:
                exchanges.append({
                    "source_message_id": r["ai_msg_id"],
                    "user_message": user_msg["content"],
                    "ai_message": r["ai_content"],
                })

        return exchanges


def extract_qa(exchange: dict) -> dict | None:
    """Use Claude CLI to extract a Q&A pair from a chat exchange."""
    prompt = EXTRACT_PROMPT.format(
        user_message=exchange["user_message"][:500],
        ai_message=exchange["ai_message"][:1000],
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None

        output = json.loads(result.stdout)
        text = output.get("result", "")

        # Try to parse JSON from the response
        # Find JSON object in the text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            return None

        qa = json.loads(text[start:end])
        if not qa or not qa.get("question") or not qa.get("answer"):
            return None

        qa["source_message_id"] = exchange["source_message_id"]
        return qa

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        logger.warning(f"Extraction failed: {e}")
        return None


def insert_knowledge(qa: dict, dry_run: bool = False) -> bool:
    """Insert a Q&A pair into knowledge_base as draft."""
    now = datetime.now(timezone.utc).isoformat()

    if dry_run:
        logger.info(f"  [DRY RUN] Would insert: {qa['category']} | {qa['question'][:60]}...")
        return True

    try:
        with get_db() as db:
            # Check for duplicates (same question text)
            existing = db.execute(
                "SELECT id FROM knowledge_base WHERE question = ?",
                (qa["question"],),
            ).fetchone()
            if existing:
                logger.info(f"  Skipped duplicate: {qa['question'][:60]}...")
                return False

            db.execute(
                """INSERT INTO knowledge_base
                   (question, answer, category, tags, source_message_id, confidence, status, scope, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 0.5, 'draft', 'all', ?, ?)""",
                (qa["question"], qa["answer"], qa["category"], qa.get("tags", ""), qa["source_message_id"], now, now),
            )
        return True
    except Exception as e:
        logger.error(f"  Insert failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Extract knowledge from helpful chat messages")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB, just show what would be extracted")
    parser.add_argument("--limit", type=int, default=200, help="Max messages to process")
    args = parser.parse_args()

    init_db()

    logger.info("Fetching helpful exchanges...")
    exchanges = get_helpful_exchanges(args.limit)
    logger.info(f"Found {len(exchanges)} helpful exchanges")

    if not exchanges:
        logger.info("No helpful feedback found yet. Ask users to rate AI responses!")
        return

    extracted = 0
    skipped = 0
    failed = 0

    for i, ex in enumerate(exchanges, 1):
        logger.info(f"[{i}/{len(exchanges)}] Processing: {ex['user_message'][:50]}...")
        qa = extract_qa(ex)
        if qa:
            if insert_knowledge(qa, args.dry_run):
                extracted += 1
            else:
                skipped += 1
        else:
            failed += 1
            logger.info("  Not extractable (contains specific data or not generalizable)")

    logger.info(f"\nDone: {extracted} extracted, {skipped} skipped (duplicate), {failed} not extractable")


if __name__ == "__main__":
    main()
