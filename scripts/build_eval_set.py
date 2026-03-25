"""Build and run an offline evaluation set for AI service quality regression testing.

Usage:
    cd angel-ai-service

    # Build evaluation set from verified KB + resolved conversations
    python scripts/build_eval_set.py build [--output data/eval_set.json]

    # Run evaluation against current AI service
    python scripts/build_eval_set.py run [--input data/eval_set.json] [--output data/eval_results.json]

Evaluation uses a structured judge (category match + keyword coverage)
instead of raw text comparison, suitable for free-form AI responses.
"""

import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.sqlite_db import get_db, init_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Build eval set
# ---------------------------------------------------------------------------

def extract_from_kb(limit: int = 200) -> list[dict]:
    """Extract test cases from verified knowledge base entries."""
    with get_db() as db:
        rows = db.execute(
            """SELECT id, question, answer, category, tags
               FROM knowledge_base
               WHERE status = 'verified'
               ORDER BY confidence DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    cases = []
    for r in rows:
        cases.append({
            "id": f"kb-{r['id']}",
            "question": r["question"],
            "expected_answer": r["answer"],
            "category": r["category"],
            "tags": r["tags"] or "",
            "source": "knowledge_base",
            "keywords": _extract_keywords(r["answer"]),
        })
    return cases


def extract_from_conversations(limit: int = 100) -> list[dict]:
    """Extract test cases from resolved conversations (feedback=correct/resolved).

    Applies generalization rules from extract_knowledge.py:
    - Skip answers with specific numbers/dates/names
    - Only keep concept/rule/process explanations
    """
    with get_db() as db:
        rows = db.execute(
            """SELECT rf.query, m.content as ai_answer, rf.route_decision
               FROM retrieval_feedback rf
               JOIN messages m ON rf.message_id = m.id
               WHERE rf.user_feedback IN ('correct', 'resolved')
                 AND m.role = 'assistant'
               ORDER BY rf.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

    cases = []
    for i, r in enumerate(rows):
        answer = r["ai_answer"]
        # Skip answers with specific numbers (time-bound data)
        if re.search(r'€\s*[\d.,]{4,}', answer):
            continue
        if re.search(r'\b\d{1,3}([.,]\d{3})+\b', answer):
            continue
        # Skip very short answers (likely data lookups)
        if len(answer) < 100:
            continue

        cases.append({
            "id": f"conv-{i}",
            "question": r["query"],
            "expected_answer": answer[:500],  # Truncate for eval
            "category": "faq",
            "tags": "",
            "source": "conversation",
            "keywords": _extract_keywords(answer),
        })
    return cases


def _extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from answer text for structured judging."""
    # Remove markdown formatting
    clean = re.sub(r'[#*|`\[\]()]', ' ', text)
    clean = re.sub(r'https?://\S+', '', clean)

    # Extract words >= 4 chars, excluding common stop words
    stop_words = {
        # English
        "the", "and", "for", "that", "this", "with", "from", "are", "was", "were",
        "been", "have", "has", "had", "will", "would", "could", "should", "can",
        "not", "but", "all", "also", "more", "some", "than", "each", "when",
        "about", "into", "over", "after", "before", "between", "under", "there",
        "their", "they", "them", "then", "what", "which", "where", "your", "your",
        # Italian
        "come", "nella", "nelle", "delle", "degli", "della", "dello", "dell",
        "sono", "essere", "viene", "ogni", "anche", "questo", "questa",
        "quello", "quella", "perché", "quando", "loro", "nostro", "nostra",
        "altro", "altra", "altri", "altre", "stato", "stata", "stati", "state",
        "tutti", "tutto", "tutta", "tutte", "molto", "molta", "molti", "molte",
        "solo", "sola", "soli", "sole", "ancora", "sempre", "dopo", "prima",
        "sopra", "sotto", "dentro", "fuori", "senza", "verso", "circa",
        "alla", "alle", "allo", "agli", "sulle", "sulla", "sullo",
        "dalla", "dalle", "dallo", "dagli", "nelle", "nello",
        "quale", "quali", "dove", "quanto", "quanta", "quanti", "quante",
        "essere", "avere", "fare", "dire", "dare", "stare", "andare",
    }

    words = re.findall(r'\b[a-zA-ZàèéìòùÀÈÉÌÒÙ]{4,}\b', clean.lower())
    # Deduplicate while preserving order, skip stop words
    seen = set()
    keywords = []
    for w in words:
        if w not in seen and w not in stop_words:
            seen.add(w)
            keywords.append(w)
    return keywords[:20]  # Top 20 keywords


def build_eval_set(output_path: str):
    """Build the evaluation set and save to JSON."""
    init_db()

    logger.info("Extracting from knowledge base...")
    kb_cases = extract_from_kb()
    logger.info(f"  Found {len(kb_cases)} KB-based cases")

    logger.info("Extracting from resolved conversations...")
    conv_cases = extract_from_conversations()
    logger.info(f"  Found {len(conv_cases)} conversation-based cases")

    all_cases = kb_cases + conv_cases

    # Deduplicate by question text
    seen_q = set()
    unique_cases = []
    for c in all_cases:
        q_norm = c["question"].lower().strip()
        if q_norm not in seen_q:
            seen_q.add(q_norm)
            unique_cases.append(c)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    eval_set = {
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_cases": len(unique_cases),
        "sources": {
            "knowledge_base": len(kb_cases),
            "conversations": len(conv_cases),
        },
        "cases": unique_cases,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(eval_set, f, indent=2, ensure_ascii=False)

    logger.info(f"Eval set saved to {output_path} ({len(unique_cases)} cases)")


# ---------------------------------------------------------------------------
# Run evaluation
# ---------------------------------------------------------------------------

def judge_response(actual: str, expected: str, keywords: list[str], category: str) -> dict:
    """Structured judge: category relevance + keyword coverage.

    Returns {score: 0-1, keyword_coverage: float, length_ratio: float, verdict: str}
    """
    if not actual or not actual.strip():
        return {"score": 0, "keyword_coverage": 0, "length_ratio": 0, "verdict": "empty"}

    # Keyword coverage: what fraction of expected keywords appear in actual
    actual_lower = actual.lower()
    matched = sum(1 for kw in keywords if kw in actual_lower) if keywords else 0
    keyword_coverage = matched / len(keywords) if keywords else 1.0

    # Length ratio (graduated penalty — tighter range)
    len_ratio = len(actual) / max(len(expected), 1)
    length_score = max(0.0, 1.0 - abs(math.log(max(len_ratio, 0.01)))) if len_ratio > 0 else 0.0

    # Combined score
    score = round(keyword_coverage * 0.7 + length_score * 0.3, 2)

    if score >= 0.7:
        verdict = "pass"
    elif score >= 0.4:
        verdict = "partial"
    else:
        verdict = "fail"

    return {
        "score": score,
        "keyword_coverage": round(keyword_coverage, 2),
        "length_ratio": round(len_ratio, 2),
        "verdict": verdict,
    }


def run_eval(input_path: str, output_path: str):
    """Run evaluation: send each question to Claude CLI, judge response."""
    with open(input_path, "r", encoding="utf-8") as f:
        eval_set = json.load(f)

    cases = eval_set["cases"]
    logger.info(f"Running evaluation on {len(cases)} cases...")

    results = []
    pass_count = 0
    partial_count = 0
    fail_count = 0
    error_count = 0

    for i, case in enumerate(cases, 1):
        logger.info(f"[{i}/{len(cases)}] {case['question'][:50]}...")

        try:
            proc = subprocess.run(
                ["claude", "-p", case["question"], "--output-format", "json"],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0:
                results.append({**case, "actual": "", "judge": {"score": 0, "verdict": "error"}, "error": proc.stderr[:200]})
                error_count += 1
                continue

            output = json.loads(proc.stdout)
            actual = output.get("result", "")

        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
            results.append({**case, "actual": "", "judge": {"score": 0, "verdict": "error"}, "error": str(e)[:200]})
            error_count += 1
            continue

        judgment = judge_response(actual, case["expected_answer"], case.get("keywords", []), case["category"])
        results.append({**case, "actual": actual[:500], "judge": judgment})

        if judgment["verdict"] == "pass":
            pass_count += 1
        elif judgment["verdict"] == "partial":
            partial_count += 1
        else:
            fail_count += 1

    # Summary
    total = len(cases)
    summary = {
        "version": eval_set["version"],
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "pass": pass_count,
        "partial": partial_count,
        "fail": fail_count,
        "error": error_count,
        "pass_rate": round(pass_count / total * 100, 1) if total > 0 else 0,
        "avg_score": round(sum(r["judge"]["score"] for r in results) / total, 2) if total > 0 else 0,
    }

    eval_results = {"summary": summary, "results": results}

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(eval_results, f, indent=2, ensure_ascii=False)

    logger.info(f"\nEvaluation complete:")
    logger.info(f"  Pass: {pass_count}/{total} ({summary['pass_rate']}%)")
    logger.info(f"  Partial: {partial_count}/{total}")
    logger.info(f"  Fail: {fail_count}/{total}")
    logger.info(f"  Error: {error_count}/{total}")
    logger.info(f"  Avg score: {summary['avg_score']}")
    logger.info(f"  Results saved to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Build and run offline evaluation set")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Build subcommand
    build_parser = subparsers.add_parser("build", help="Build eval set from KB and conversations")
    build_parser.add_argument("--output", default="data/eval_set.json", help="Output path")

    # Run subcommand
    run_parser = subparsers.add_parser("run", help="Run eval against Claude CLI")
    run_parser.add_argument("--input", default="data/eval_set.json", help="Eval set path")
    run_parser.add_argument("--output", default="data/eval_results.json", help="Results path")

    args = parser.parse_args()

    if args.command == "build":
        build_eval_set(args.output)
    elif args.command == "run":
        run_eval(args.input, args.output)


if __name__ == "__main__":
    main()
