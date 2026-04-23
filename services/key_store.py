"""Encrypted Gemini API key pool.

Keys are fernet-encrypted at rest in gemini_api_keys. The fernet key is
HKDF-SHA256-derived from SERVICE_TOKEN_SECRET with a per-version info
string, so rotating the secret in the future can be supported by keeping
both versions readable during migration.

Rotation contract (get_active_key):
    - Picks is_active=1 rows whose cooldown (if any) has expired.
    - Prefers the oldest last_used_at (fair rotation across the pool).
    - The SELECT + UPDATE last_used_at run inside BEGIN IMMEDIATE so
      concurrent workers cannot pick the same row.
    - A row whose ciphertext fails to decrypt is auto-deactivated and
      the caller retries for the next candidate.

The plaintext key only lives inside this module during a single call.
Outside of get_active_key / list_keys_masked it is never materialized.
"""
from __future__ import annotations

import base64
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from config import settings
from db.sqlite_db import get_connection, get_db

logger = logging.getLogger(__name__)

_HKDF_SALT = b"angel-ai-service:gemini-key-pool:v1"
_CURRENT_KEY_VERSION = 1


def _derive_fernet_key(version: int = _CURRENT_KEY_VERSION) -> bytes:
    secret = settings.SERVICE_TOKEN_SECRET.encode("utf-8")
    if not secret or secret == b"change-me-in-production":
        logger.critical("SERVICE_TOKEN_SECRET is default/empty; key pool encryption is insecure")
    info = f"fernet:v{version}".encode("utf-8")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=_HKDF_SALT, info=info)
    return base64.urlsafe_b64encode(hkdf.derive(secret))


def _fernet(version: int = _CURRENT_KEY_VERSION) -> Fernet:
    return Fernet(_derive_fernet_key(version))


def _mask(plain: str) -> str:
    if len(plain) < 10:
        return "*" * len(plain)
    return f"{plain[:6]}...{plain[-4:]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _next_utc_midnight() -> str:
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()


def _decrypt(ciphertext: str, version: int) -> str:
    return _fernet(version).decrypt(ciphertext.encode("ascii")).decode("utf-8")


def add_key(label: str, plain: str, created_by: Optional[str]) -> int:
    """Insert a new encrypted key. Raises sqlite3.IntegrityError on label
    conflict; the router layer maps that to HTTP 409."""
    enc = _fernet().encrypt(plain.encode("utf-8")).decode("ascii")
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO gemini_api_keys
                 (label, key_encrypted, key_version, is_active, created_at, created_by)
               VALUES (?, ?, ?, 1, ?, ?)""",
            (label, enc, _CURRENT_KEY_VERSION, _now_iso(), created_by),
        )
        return cur.lastrowid


def list_keys_masked() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, label, key_encrypted, key_version, is_active,
                      cooldown_until, last_used_at, created_at, created_by
               FROM gemini_api_keys ORDER BY id"""
        ).fetchall()
    out = []
    for r in rows:
        try:
            plain = _decrypt(r["key_encrypted"], r["key_version"])
            masked = _mask(plain)
        except InvalidToken:
            masked = "<decrypt-failed>"
        out.append({
            "id": r["id"],
            "label": r["label"],
            "masked": masked,
            "key_version": r["key_version"],
            "is_active": bool(r["is_active"]),
            "cooldown_until": r["cooldown_until"],
            "last_used_at": r["last_used_at"],
            "created_at": r["created_at"],
            "created_by": r["created_by"],
        })
    return out


def delete_key(key_id: int) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM gemini_api_keys WHERE id = ?", (key_id,))
        return cur.rowcount > 0


def _deactivate(conn: sqlite3.Connection, key_id: int, reason: str) -> None:
    conn.execute("UPDATE gemini_api_keys SET is_active = 0 WHERE id = ?", (key_id,))
    logger.warning("key id=%s auto-deactivated: %s", key_id, reason)


def mark_cooldown(key_id: int, until_iso: Optional[str] = None) -> None:
    """Cool a key down until the given ISO-8601 UTC timestamp (defaults
    to next UTC midnight, matching typical Gemini daily quota reset)."""
    target = until_iso or _next_utc_midnight()
    with get_db() as conn:
        conn.execute(
            "UPDATE gemini_api_keys SET cooldown_until = ? WHERE id = ?",
            (target, key_id),
        )


def get_active_key(exclude_ids: Optional[set[int]] = None) -> Optional[tuple[int, str]]:
    """Atomically pick the next usable key and stamp last_used_at.

    exclude_ids lets the rotation wrapper skip keys it already tried in
    the current request (so the pool is walked at most once per request).
    Returns (key_id, plaintext) or None if the pool is drained.
    """
    excluded = exclude_ids or set()
    now = _now_iso()
    conn = get_connection()
    try:
        # Deactivate-on-decrypt-failure may need to span multiple rounds,
        # so the outer loop keeps pulling candidates until one decrypts
        # or the filter set empties.
        while True:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if excluded:
                    placeholders = ",".join("?" * len(excluded))
                    query = (
                        "SELECT id, key_encrypted, key_version FROM gemini_api_keys "
                        "WHERE is_active = 1 "
                        "  AND (cooldown_until IS NULL OR cooldown_until <= ?) "
                        f"  AND id NOT IN ({placeholders}) "
                        "ORDER BY COALESCE(last_used_at, '0000-01-01') ASC, id ASC "
                        "LIMIT 1"
                    )
                    params = (now, *excluded)
                else:
                    query = (
                        "SELECT id, key_encrypted, key_version FROM gemini_api_keys "
                        "WHERE is_active = 1 "
                        "  AND (cooldown_until IS NULL OR cooldown_until <= ?) "
                        "ORDER BY COALESCE(last_used_at, '0000-01-01') ASC, id ASC "
                        "LIMIT 1"
                    )
                    params = (now,)
                row = conn.execute(query, params).fetchone()
                if not row:
                    conn.commit()
                    return None
                conn.execute(
                    "UPDATE gemini_api_keys SET last_used_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            try:
                plain = _decrypt(row["key_encrypted"], row["key_version"])
                return (row["id"], plain)
            except InvalidToken:
                # Auto-heal: mark as broken and keep walking. Add to the
                # exclude set so the next SELECT skips it too.
                with get_db() as healer:
                    _deactivate(healer, row["id"], "InvalidToken on decrypt")
                excluded = excluded | {row["id"]}
                continue
    finally:
        conn.close()
