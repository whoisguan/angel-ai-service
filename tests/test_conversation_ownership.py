"""H1: Conversation ownership tests — read and write paths."""

import sqlite3
from unittest.mock import patch

import pytest

from models.schemas import UserContext, UserScope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user_ctx(user_id: int) -> UserContext:
    return UserContext(
        user_id=user_id,
        source_system="angel-kpi",
        roles=["store_manager"],
        permissions=["view_kpi"],
        scope=UserScope(store_ids=[1], department_codes=[]),
        locale="it",
    )


@pytest.fixture
def mem_db():
    """Create an in-memory SQLite database with the schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            source_system TEXT NOT NULL DEFAULT 'angel-kpi',
            title TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
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
        CREATE TABLE usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source_system TEXT NOT NULL,
            cost_usd REAL DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            model TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
    """)
    # Seed: conversation owned by user 1
    conn.execute(
        "INSERT INTO conversations (id, user_id, source_system, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("conv-aaa", 1, "angel-kpi", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        ("msg-001", "conv-aaa", "user", "Hello", "2026-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        ("msg-002", "conv-aaa", "assistant", "Hi there", "2026-01-01T00:00:01"),
    )
    conn.commit()
    yield conn
    conn.close()


def _patch_db(mem_db):
    """Return a patch context that makes get_db() yield mem_db."""
    from contextlib import contextmanager

    @contextmanager
    def fake_get_db():
        yield mem_db

    return patch("services.chat_service.get_db", fake_get_db)


# ---------------------------------------------------------------------------
# READ PATH: _get_conversation_history
# ---------------------------------------------------------------------------

class TestReadPath:
    def test_owner_can_read_own_conversation(self, mem_db):
        from services.chat_service import _get_conversation_history

        with _patch_db(mem_db):
            history = _get_conversation_history("conv-aaa", user_id=1)

        assert len(history) == 2
        assert history[0]["content"] == "Hello"
        assert history[1]["content"] == "Hi there"

    def test_other_user_gets_empty_history(self, mem_db):
        from services.chat_service import _get_conversation_history

        with _patch_db(mem_db):
            history = _get_conversation_history("conv-aaa", user_id=999)

        assert history == []

    def test_nonexistent_conversation_returns_empty(self, mem_db):
        from services.chat_service import _get_conversation_history

        with _patch_db(mem_db):
            history = _get_conversation_history("conv-nonexistent", user_id=1)

        assert history == []


# ---------------------------------------------------------------------------
# WRITE PATH: _save_message
# ---------------------------------------------------------------------------

class TestWritePath:
    def test_owner_can_write_to_own_conversation(self, mem_db):
        from services.chat_service import _save_message

        ctx = _make_user_ctx(user_id=1)
        with _patch_db(mem_db):
            _save_message("conv-aaa", "msg-new", "user", "New message", ctx)

        row = mem_db.execute(
            "SELECT * FROM messages WHERE id = 'msg-new'"
        ).fetchone()
        assert row is not None
        assert row["content"] == "New message"

    def test_other_user_cannot_write_to_conversation(self, mem_db):
        from services.chat_service import _save_message

        ctx = _make_user_ctx(user_id=999)
        with _patch_db(mem_db):
            with pytest.raises(ValueError, match="ownership mismatch"):
                _save_message("conv-aaa", "msg-bad", "user", "Hijack", ctx)

        # Verify no message was written
        row = mem_db.execute(
            "SELECT * FROM messages WHERE id = 'msg-bad'"
        ).fetchone()
        assert row is None

    def test_new_conversation_created_for_new_id(self, mem_db):
        from services.chat_service import _save_message

        ctx = _make_user_ctx(user_id=42)
        with _patch_db(mem_db):
            _save_message("conv-new", "msg-first", "user", "First message", ctx)

        conv = mem_db.execute(
            "SELECT * FROM conversations WHERE id = 'conv-new'"
        ).fetchone()
        assert conv is not None
        assert conv["user_id"] == 42

    def test_write_updates_conversation_timestamp(self, mem_db):
        from services.chat_service import _save_message

        old = mem_db.execute(
            "SELECT updated_at FROM conversations WHERE id = 'conv-aaa'"
        ).fetchone()["updated_at"]

        ctx = _make_user_ctx(user_id=1)
        with _patch_db(mem_db):
            _save_message("conv-aaa", "msg-ts", "user", "Timestamp test", ctx)

        new = mem_db.execute(
            "SELECT updated_at FROM conversations WHERE id = 'conv-aaa'"
        ).fetchone()["updated_at"]
        assert new != old
