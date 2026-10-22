"""API keys. Every user can have up to MAX_KEYS_PER_USER of them.

A key is shown once, when it's created. The database keeps only its SHA-256 and the first few
characters: a key is 32 random bytes, so a plain hash is enough to make a stolen database useless.
"""

import hashlib
import secrets
import time
from dataclasses import dataclass

from fastapi.security import APIKeyHeader

from . import db

MAX_KEYS_PER_USER = 3
KEY_PREFIX = "sk_"
SHOWN_PREFIX_LENGTH = len(KEY_PREFIX) + 6  # "sk_" plus 6 characters, enough to tell keys apart
NAME_MAX = 40

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class TooManyKeys(Exception):
    pass


@dataclass(frozen=True)
class ApiKey:
    id: int
    user_id: int
    prefix: str
    name: str
    created_at: float
    last_used_at: float | None
    requests: int


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _api_key(row) -> ApiKey:
    return ApiKey(
        row["id"], row["user_id"], row["prefix"], row["name"], row["created_at"], row["last_used_at"], row["requests"]
    )


def create_key(user_id: int, name: str) -> tuple[str, ApiKey]:
    """Make a new key. Returns the key itself, which is never available again, and its record.
    Raises TooManyKeys when the user already has MAX_KEYS_PER_USER."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    name = name.strip()[:NAME_MAX] or "Untitled key"
    with db.transaction() as conn:  # count and insert in one transaction, so two clicks can't make a 4th key
        (count,) = conn.execute("SELECT COUNT(*) FROM api_keys WHERE user_id = ?", (user_id,)).fetchone()
        if count >= MAX_KEYS_PER_USER:
            raise TooManyKeys
        cursor = conn.execute(
            "INSERT INTO api_keys (user_id, key_hash, prefix, name, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, hash_key(key), key[:SHOWN_PREFIX_LENGTH], name, time.time()),
        )
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return key, _api_key(row)


def list_keys(user_id: int) -> list[ApiKey]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at", (user_id,)).fetchall()
    return [_api_key(row) for row in rows]


def revoke_key(user_id: int, key_id: int) -> bool:
    """Delete one of the user's keys. It stops working right away. False if there was no such key."""
    with db.connect() as conn:
        cursor = conn.execute("DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, user_id))
    return cursor.rowcount > 0


def find_key(key: str | None) -> ApiKey | None:
    """The record for a key, or None if it's missing, malformed or unknown. Looked up by hash, so the
    lookup time says nothing about how close a guess was."""
    if not key or not key.startswith(KEY_PREFIX) or len(key) > 100:
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM api_keys WHERE key_hash = ?", (hash_key(key),)).fetchone()
    return _api_key(row) if row else None
