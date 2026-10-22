"""Accounts: sign-up, passwords, login sessions, and the usage numbers shown on the dashboard.

Passwords are hashed with scrypt from the standard library. A login session is a random token in
a cookie; the database keeps only its SHA-256, so a copy of the database can't be used to log in.
"""

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import db

SESSION_COOKIE = "session"
SESSION_SECONDS = 30 * 24 * 3600
PASSWORD_MIN, PASSWORD_MAX = 8, 128
EMAIL_MAX = 254

# scrypt cost: about 16 MB of memory and a few tens of milliseconds per hash.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1

EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class EmailTaken(Exception):
    pass


@dataclass(frozen=True)
class User:
    id: int
    email: str
    created_at: float


@dataclass(frozen=True)
class Session:
    user: User
    csrf_token: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---- Input rules -------------------------------------------------------------------------------


def normalize_email(email: str) -> str | None:
    """The email lowercased and trimmed, or None if it doesn't look like an address."""
    email = email.strip().lower()
    if len(email) > EMAIL_MAX or not EMAIL_PATTERN.fullmatch(email):
        return None
    return email


def password_problem(password: str) -> str | None:
    """What's wrong with a new password, or None if it's acceptable."""
    if len(password) < PASSWORD_MIN:
        return f"The password needs at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return f"The password can have at most {PASSWORD_MAX} characters."
    return None


# ---- Passwords ---------------------------------------------------------------------------------


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def hash_password(password: str) -> str:
    """Stored as scrypt$n$r$p$salt$hash, so the cost can be raised later without breaking old hashes."""
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    b64 = base64.b64encode
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except ValueError:
        return False
    return secrets.compare_digest(actual, base64.b64decode(digest))


# A real hash to check against when the email is unknown, so a login takes as long for an unknown
# email as for a wrong password and doesn't reveal which emails have accounts.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# ---- Users -------------------------------------------------------------------------------------


def _user(row) -> User:
    return User(row["id"], row["email"], row["created_at"])


def create_user(email: str, password: str) -> User:
    """`email` must already be normalized and `password` checked with password_problem()."""
    password_hash = hash_password(password)
    with db.transaction() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise EmailTaken(email)
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)", (email, password_hash, time.time())
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _user(row)


def authenticate(email: str, password: str) -> User | None:
    """The user if the email and password match, otherwise None."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    if row is None:
        verify_password(password, _DUMMY_HASH)
        return None
    return _user(row) if verify_password(password, row["password_hash"]) else None


def check_password(user_id: int, password: str) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is not None and verify_password(password, row["password_hash"])


def change_password(user_id: int, new_password: str, keep_session: str | None) -> None:
    """Set a new password and log out every other session, in case the old password had leaked."""
    password_hash = hash_password(new_password)
    with db.transaction() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, _sha256(keep_session) if keep_session else ""),
        )


def delete_user(user_id: int) -> None:
    """Delete the account. Its keys, sessions and usage go with it (ON DELETE CASCADE)."""
    with db.connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ---- Sessions ----------------------------------------------------------------------------------


def start_session(user_id: int) -> str:
    """Log the user in. Returns the token for the cookie; only its hash is stored."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))  # tidy up expired ones now and then
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) VALUES (?, ?, ?, ?)",
            (_sha256(token), user_id, secrets.token_urlsafe(32), now + SESSION_SECONDS),
        )
    return token


def find_session(token: str | None) -> Session | None:
    if not token:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT users.*, sessions.csrf_token FROM sessions JOIN users ON users.id = sessions.user_id"
            " WHERE sessions.token_hash = ? AND sessions.expires_at > ?",
            (_sha256(token), time.time()),
        ).fetchone()
    return Session(_user(row), row["csrf_token"]) if row else None


def end_session(token: str | None) -> None:
    if not token:
        return
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha256(token),))


# ---- Usage -------------------------------------------------------------------------------------


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def record_usage(user_id: int, key_id: int | None, cost: int) -> None:
    """Count `cost` requests for today, and for the key when the request came with one."""
    now = time.time()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage (user_id, day, requests) VALUES (?, ?, ?)"
            " ON CONFLICT (user_id, day) DO UPDATE SET requests = requests + excluded.requests",
            (user_id, _today(), cost),
        )
        if key_id is not None:
            conn.execute(
                "UPDATE api_keys SET requests = requests + ?, last_used_at = ? WHERE id = ?", (cost, now, key_id)
            )


def usage_by_day(user_id: int, days: int = 7) -> list[tuple[str, int]]:
    """Requests per UTC day for the last `days` days, oldest first, including days with none."""
    today = datetime.now(UTC).date()
    wanted = [(today - timedelta(days=i)).isoformat() for i in reversed(range(days))]
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT day, requests FROM usage WHERE user_id = ? AND day >= ?", (user_id, wanted[0])
        ).fetchall()
    counts = {row["day"]: row["requests"] for row in rows}
    return [(day, counts.get(day, 0)) for day in wanted]


def total_usage(user_id: int) -> int:
    with db.connect() as conn:
        row = conn.execute("SELECT COALESCE(SUM(requests), 0) AS n FROM usage WHERE user_id = ?", (user_id,)).fetchone()
    return row["n"]
