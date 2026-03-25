"""Generate monthly AI service quality report.

Usage:
    cd angel-ai-service
    python scripts/monthly_report.py [--month 2026-03] [--output docs/reports/]

Analyzes retrieval_feedback, usage_log, and feedback tables to produce
a markdown report with failure analysis, retrieval metrics, and cost summary.
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.sqlite_db import get_db, init_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Basic PII patterns to scrub from report output
_PII_PATTERNS = [
    (re.compile(r'[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]'), '[CODICE_FISCALE]'),  # Codice Fiscale
    (re.compile(r'[A-Z]{2}\d{2}\s?[A-Z]\d{10,22}'), '[IBAN]'),
    (re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b'), '[EMAIL]'),
    (re.compile(r'\b\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{1,4}\b'), '[CARD]'),
    (re.compile(r'\+?\d{2,3}[\s.-]?\d{3,4}[\s.-]?\d{4,7}\b'), '[PHONE]'),
]


def _scrub_pii(text: str) -> str:
    """Remove basic PII patterns from text."""
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _get_month_range(month_str: str) -> tuple[str, str]:
    """Return (start, end) ISO date strings for a month like '2026-03'."""
    year, mon = map(int, month_str.split("-"))
    start = f"{year:04d}-{mon:02d}-01"
    if mon == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{mon + 1:02d}-01"
    return start, end


def get_feedback_distribution(start: str, end: str) -> dict:
    """Count retrieval feedback by category."""
    with get_db() as db:
        rows = db.execute(
            """SELECT user_feedback, COUNT(*) as cnt
               FROM retrieval_feedback
               WHERE created_at >= ? AND created_at < ?
               GROUP BY user_feedback
               ORDER BY cnt DESC""",
            (start, end),
        ).fetchall()
    return {r["user_feedback"] or "no_feedback": r["cnt"] for r in rows}


def get_top_failures(start: str, end: str, limit: int = 10) -> list[dict]:
    """Get top N failed/unresolved queries."""
    with get_db() as db:
        rows = db.execute(
            """SELECT query, route_decision, user_feedback, COUNT(*) as cnt
               FROM retrieval_feedback
               WHERE created_at >= ? AND created_at < ?
                 AND user_feedback IN ('incorrect', 'unresolved')
               GROUP BY query, route_decision, user_feedback
               ORDER BY cnt DESC
               LIMIT ?""",
            (start, end, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_retrieval_hit_rate(start: str, end: str) -> dict:
    """Calculate retrieval hit rate (queries that got KB matches vs total)."""
    with get_db() as db:
        total = db.execute(
            "SELECT COUNT(*) FROM retrieval_feedback WHERE created_at >= ? AND created_at < ?",
            (start, end),
        ).fetchone()[0]

        with_hits = db.execute(
            """SELECT COUNT(*) FROM retrieval_feedback
               WHERE created_at >= ? AND created_at < ?
                 AND retrieved_kb_ids IS NOT NULL AND retrieved_kb_ids != '[]'""",
            (start, end),
        ).fetchone()[0]

        route_dist = db.execute(
            """SELECT route_decision, COUNT(*) as cnt
               FROM retrieval_feedback
               WHERE created_at >= ? AND created_at < ?
               GROUP BY route_decision""",
            (start, end),
        ).fetchall()

    return {
        "total_queries": total,
        "with_kb_hits": with_hits,
        "hit_rate": round(with_hits / total * 100, 1) if total > 0 else 0,
        "route_distribution": {r["route_decision"]: r["cnt"] for r in route_dist},
    }


def get_cost_summary(start: str, end: str) -> dict:
    """Get monthly cost and usage from usage_log."""
    with get_db() as db:
        row = db.execute(
            """SELECT
                COUNT(*) as total_requests,
                COALESCE(SUM(cost_usd), 0) as total_cost,
                COALESCE(AVG(cost_usd), 0) as avg_cost,
                COALESCE(AVG(duration_ms), 0) as avg_duration,
                COUNT(DISTINCT user_id) as unique_users
            FROM usage_log
            WHERE created_at >= ? AND created_at < ?""",
            (start, end),
        ).fetchone()
    return {
        "total_requests": row["total_requests"],
        "total_cost_usd": round(row["total_cost"], 4),
        "avg_cost_per_request": round(row["avg_cost"], 4),
        "avg_duration_ms": int(row["avg_duration"]),
        "unique_users": row["unique_users"],
    }


def get_user_feedback_summary(start: str, end: str) -> dict:
    """Get user feedback (thumbs up/down) from feedback table."""
    with get_db() as db:
        rows = db.execute(
            """SELECT rating, COUNT(*) as cnt
               FROM feedback
               WHERE created_at >= ? AND created_at < ?
               GROUP BY rating
               ORDER BY cnt DESC""",
            (start, end),
        ).fetchall()
    return {r["rating"]: r["cnt"] for r in rows}


def generate_report(month_str: str) -> str:
    """Generate the full markdown report."""
    start, end = _get_month_range(month_str)

    feedback_dist = get_feedback_distribution(start, end)
    top_failures = get_top_failures(start, end)
    hit_rate = get_retrieval_hit_rate(start, end)
    cost = get_cost_summary(start, end)
    user_fb = get_user_feedback_summary(start, end)

    lines = [
        f"# AI Service Monthly Report — {month_str}",
        f"\n> Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Usage Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total requests | {cost['total_requests']} |",
        f"| Unique users | {cost['unique_users']} |",
        f"| Total cost | ${cost['total_cost_usd']} |",
        f"| Avg cost/request | ${cost['avg_cost_per_request']} |",
        f"| Avg duration | {cost['avg_duration_ms']}ms |",
        "",
        "## User Feedback (thumbs)",
        "",
        f"| Rating | Count |",
        f"|--------|-------|",
    ]
    for rating, cnt in user_fb.items():
        lines.append(f"| {rating} | {cnt} |")
    if not user_fb:
        lines.append("| (no feedback yet) | - |")

    lines.extend([
        "",
        "## Retrieval Metrics",
        "",
        f"- Total queries: {hit_rate['total_queries']}",
        f"- Queries with KB hits: {hit_rate['with_kb_hits']} ({hit_rate['hit_rate']}%)",
        f"- Route distribution: {json.dumps(hit_rate['route_distribution'])}",
        "",
        "## Retrieval Feedback Distribution",
        "",
        f"| Feedback | Count |",
        f"|----------|-------|",
    ])
    for fb, cnt in feedback_dist.items():
        lines.append(f"| {fb} | {cnt} |")
    if not feedback_dist:
        lines.append("| (no feedback yet) | - |")

    lines.extend([
        "",
        "## Top 10 Failed/Unresolved Queries",
        "",
    ])
    if top_failures:
        lines.append("| # | Query (truncated) | Route | Feedback | Count |")
        lines.append("|---|-------------------|-------|----------|-------|")
        for i, f in enumerate(top_failures, 1):
            q = _scrub_pii(f["query"][:60]).replace("|", "\\|")
            lines.append(f"| {i} | {q} | {f['route_decision']} | {f['user_feedback']} | {f['cnt']} |")
    else:
        lines.append("No failed queries this month.")

    lines.extend([
        "",
        "---",
        f"*Report generated from angel-ai-service SQLite database.*",
    ])

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate monthly AI service quality report")
    parser.add_argument("--month", default=datetime.now().strftime("%Y-%m"),
                        help="Month to report (YYYY-MM format, default: current month)")
    parser.add_argument("--output", default="docs/reports",
                        help="Output directory (default: docs/reports/)")
    args = parser.parse_args()

    init_db()

    logger.info(f"Generating report for {args.month}...")
    report = generate_report(args.month)

    os.makedirs(args.output, exist_ok=True)
    filepath = os.path.join(args.output, f"{args.month}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report)

    logger.info(f"Report saved to {filepath}")
    print(f"\n{report}")


if __name__ == "__main__":
    main()
