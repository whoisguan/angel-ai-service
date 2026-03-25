"""SQLite database for conversation history, feedback, usage tracking, and knowledge base."""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from config import settings

logger = logging.getLogger(__name__)


def _ensure_db_dir():
    db_dir = os.path.dirname(settings.SQLITE_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    """Get a SQLite connection with row factory."""
    _ensure_db_dir()
    conn = sqlite3.connect(settings.SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    _ensure_db_dir()
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                source_system TEXT NOT NULL DEFAULT 'angel-kpi',
                title TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_conv_user
                ON conversations(source_system, user_id);

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                cost_usd REAL DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                model TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );

            CREATE INDEX IF NOT EXISTS idx_msg_conv
                ON messages(conversation_id);

            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                rating TEXT NOT NULL CHECK(rating IN ('helpful','not_helpful','wrong','harmful')),
                comment TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (message_id) REFERENCES messages(id)
            );

            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                source_system TEXT NOT NULL,
                cost_usd REAL DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                model TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_usage_date
                ON usage_log(created_at);

            -- Knowledge base for AI learning (Phase 1a)
            CREATE TABLE IF NOT EXISTS knowledge_base (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                category TEXT NOT NULL CHECK(category IN ('glossary','rule','faq','process')),
                tags TEXT,
                source_message_id TEXT,
                confidence REAL DEFAULT 0.5,
                status TEXT DEFAULT 'draft' CHECK(status IN ('draft','verified','archived')),
                scope TEXT DEFAULT 'all',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                verified_by TEXT,
                verified_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_kb_status
                ON knowledge_base(status);

            -- Retrieval feedback for learning loop
            CREATE TABLE IF NOT EXISTS retrieval_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                retrieved_kb_ids TEXT,
                route_decision TEXT CHECK(route_decision IN ('static','dynamic','hybrid')),
                user_feedback TEXT CHECK(user_feedback IN ('correct','incorrect','partial','resolved','unresolved')),
                prompt_version TEXT,
                mcp_tools_used TEXT,
                message_id TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_rf_feedback
                ON retrieval_feedback(user_feedback, created_at);

            -- Prompt versions for versioned system prompts
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_tag TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL,
                description TEXT,
                is_active INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                created_by TEXT
            );

            -- Retrieval cache for FTS5 results (scoped)
            CREATE TABLE IF NOT EXISTS retrieval_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_key TEXT NOT NULL UNIQUE,
                query TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                kb_ids TEXT NOT NULL,
                kb_scores TEXT NOT NULL,
                hit_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_rc_expires
                ON retrieval_cache(expires_at);

            -- User profiles for question pattern tracking (Phase 3)
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                source_system TEXT NOT NULL DEFAULT 'angel-kpi',
                top_topics TEXT DEFAULT '{}',
                question_count INTEGER DEFAULT 0,
                avg_questions_per_day REAL DEFAULT 0,
                preferred_language TEXT DEFAULT 'it',
                created_at TEXT,
                last_active TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, source_system)
            );

            -- Cross-conversation memory (Phase 3)
            CREATE TABLE IF NOT EXISTS user_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                source_system TEXT NOT NULL DEFAULT 'angel-kpi',
                memory_key TEXT NOT NULL,
                content TEXT NOT NULL,
                source_conversation_id TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                is_deleted INTEGER DEFAULT 0 CHECK(is_deleted IN (0, 1)),
                deleted_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_um_user_active
                ON user_memories(user_id, source_system, is_deleted, expires_at);

            -- Knowledge base audit trail (Phase 3)
            CREATE TABLE IF NOT EXISTS kb_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kb_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('created','verified','archived','updated','deleted')),
                old_status TEXT,
                new_status TEXT,
                changed_by TEXT,
                changed_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_kba_kb
                ON kb_audit_log(kb_id, changed_at);

            -- Rejected queries log (injection/rate-limit)
            CREATE TABLE IF NOT EXISTS rejected_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                source_system TEXT,
                reason TEXT NOT NULL CHECK(reason IN ('injection','rate_limit_daily','rate_limit_concurrent')),
                query_preview TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_rq_date
                ON rejected_queries(created_at);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_um_user_key_active
                ON user_memories(user_id, source_system, memory_key) WHERE is_deleted = 0;
        """)
        # FTS5 virtual table for knowledge base search (separate statement)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts
            USING fts5(question, answer, tags, content=knowledge_base, content_rowid=id)
        """)
        # Triggers to keep FTS5 in sync with knowledge_base
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS kb_fts_ai AFTER INSERT ON knowledge_base BEGIN
                INSERT INTO kb_fts(rowid, question, answer, tags)
                VALUES (NEW.id, NEW.question, NEW.answer, NEW.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS kb_fts_ad AFTER DELETE ON knowledge_base BEGIN
                INSERT INTO kb_fts(kb_fts, rowid, question, answer, tags)
                VALUES ('delete', OLD.id, OLD.question, OLD.answer, OLD.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS kb_fts_au AFTER UPDATE ON knowledge_base BEGIN
                INSERT INTO kb_fts(kb_fts, rowid, question, answer, tags)
                VALUES ('delete', OLD.id, OLD.question, OLD.answer, OLD.tags);
                INSERT INTO kb_fts(rowid, question, answer, tags)
                VALUES (NEW.id, NEW.question, NEW.answer, NEW.tags);
            END;
        """)
        # Migrations: add columns to existing tables (safe for both new and existing DBs)
        _migrate_add_column(conn, "retrieval_feedback", "message_id", "TEXT")

        # Create indexes that depend on migrated columns (must run AFTER ALTER TABLE)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rf_message ON retrieval_feedback(message_id)")

        conn.commit()
    finally:
        conn.close()


def _migrate_add_column(conn: sqlite3.Connection, table: str, column: str, col_type: str):
    """Safely add a column to an existing table. No-op if column already exists."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        logger.info(f"Migration: added {column} to {table}")
    except sqlite3.OperationalError:
        pass  # column already exists
