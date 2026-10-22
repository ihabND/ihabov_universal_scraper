"""The SQLite database: users, their API keys, login sessions and daily usage.

One file, set with DATABASE_PATH. On Fly.io it lives on a volume mounted at /data, so accounts
survive restarts and deploys. The tables are created on first use.
"""

import logging
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

PATH = os.environ.get("DATABASE_PATH", "data/scraper.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT    NOT NULL UNIQUE,  -- stored lowercased
    password_hash TEXT    NOT NULL,
    created_at    REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash      TEXT    NOT NULL UNIQUE,  -- SHA-256 of the key; the key itself is never stored
    prefix        TEXT    NOT NULL,         -- the first characters, to tell keys apart on the dashboard
    name          TEXT    NOT NULL,
    created_at    REAL    NOT NULL,
    last_used_at  REAL,
    requests      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS api_keys_user ON api_keys(user_id);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash    TEXT    PRIMARY KEY,      -- SHA-256 of the cookie value
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token    TEXT    NOT NULL,
    expires_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS usage (
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day           TEXT    NOT NULL,         -- UTC date, YYYY-MM-DD
    requests      INTEGER NOT NULL,
    PRIMARY KEY (user_id, day)
);
"""

_ready: set[str] = set()
_ready_lock = threading.Lock()


def _prepare(conn: sqlite3.Connection, path: str) -> None:
    """Create the tables the first time this process opens the database."""
    with _ready_lock:
        if path in _ready:
            return
        conn.execute("PRAGMA journal_mode = WAL")  # readers don't wait for a writer
        conn.executescript(SCHEMA)
        _ready.add(path)
        logger.info("Database ready at %s", path)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """A connection in autocommit mode: every statement commits on its own. Use transaction() when
    several statements have to happen together. A connection is cheap, so each call opens its own,
    which also keeps connections out of the worker threads' way."""
    path = PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _prepare(conn, path)
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """A write transaction, taken right away (BEGIN IMMEDIATE), so a check followed by a write, like
    "fewer than 3 keys, then add one", can't interleave with another request doing the same."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
