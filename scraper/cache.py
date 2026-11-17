"""Redis-backed caches. Everything here is best effort: if Redis is missing, slow or down,
the scraper keeps working, just without the cache.

Three kinds of entries, all with a TTL:
- results:   the finished API response, per URL, mode and browser option
- pages:     the fetched page itself, per URL and browser option, shared by HTML and JSON mode
- flags:     small facts like "this site blocks plain requests"
Results and pages are zlib-compressed: HTML shrinks to a fraction of its size.
"""

import json
import logging
import os
import threading
import time
import zlib

import redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600  # successful pages: 10 minutes
FAILED_CACHE_TTL_SECONDS = 30  # pages that returned 4xx/5xx: 30 seconds
MAX_CACHED_BYTES = 4 * 1024 * 1024  # compressed; anything bigger is not worth a Redis round trip
REDIS_PAUSE_SECONDS = 30  # after a Redis error, skip Redis this long instead of timing out on every call
KEY_PREFIX = "scrape:v2"  # v2: compressed values; older uncompressed entries are simply never read

REDIS_URL = os.environ.get("REDIS_URL")

# Values are bytes (compressed), so no decode_responses.
# Short timeouts: if Redis is slow or down, skip the cache instead of hanging the request.
redis_client = redis.Redis.from_url(REDIS_URL, socket_timeout=1, socket_connect_timeout=1) if REDIS_URL else None
if redis_client is None:
    logger.warning("REDIS_URL is not set, caching is disabled.")

_paused_until = 0.0


def _redis():
    """The client, or None while Redis is missing or paused after an error."""
    if redis_client is None or time.monotonic() < _paused_until:
        return None
    return redis_client


def _failed(action: str, error: Exception) -> None:
    global _paused_until
    _paused_until = time.monotonic() + REDIS_PAUSE_SECONDS
    logger.warning("Redis %s failed, skipping Redis for %d s: %s", action, REDIS_PAUSE_SECONDS, error)


def _get(key: str) -> bytes | None:
    client = _redis()
    if client is None:
        return None
    try:
        return client.get(key)
    except RedisError as e:
        _failed("read", e)
        return None


def _set(key: str, value: bytes, ttl: int) -> None:
    client = _redis()
    if client is None:
        return
    if len(value) > MAX_CACHED_BYTES:
        logger.info("Not caching %s: %d bytes compressed", key, len(value))
        return
    try:
        client.set(key, value, ex=ttl)
    except RedisError as e:
        _failed("write", e)


def redis_status() -> str:
    """'off' when no Redis is configured, 'paused' after a recent error, otherwise 'ok' or 'down'."""
    if redis_client is None:
        return "off"
    if time.monotonic() < _paused_until:
        return "paused"
    try:
        redis_client.ping()
        return "ok"
    except RedisError:
        return "down"


def _ttl(ok: bool) -> int:
    return CACHE_TTL_SECONDS if ok else FAILED_CACHE_TTL_SECONDS


# ---- Results -------------------------------------------------------------------------------


def result_key(url: str, mode: str, browser: str) -> str:
    # The browser option is part of the key: a page that came back blocked with browser=never
    # must not be served to a browser=auto request, and browser=always must not get an http result.
    return f"{KEY_PREFIX}:result:{mode}:{browser}:{url}"


def get_cached(url: str, mode: str, browser: str) -> str | None:
    """The cached response JSON, or None."""
    value = _get(result_key(url, mode, browser))
    if value is None:
        return None
    try:
        return zlib.decompress(value).decode("utf-8")
    except (zlib.error, UnicodeDecodeError) as e:
        logger.warning("Ignoring a broken cache entry for %s: %s", url, e)
        return None


def set_cached(url: str, mode: str, browser: str, value: str, ok: bool) -> None:
    _set(result_key(url, mode, browser), zlib.compress(value.encode("utf-8"), 6), _ttl(ok))


# ---- Pages ---------------------------------------------------------------------------------


def page_key(url: str, browser: str) -> str:
    return f"{KEY_PREFIX}:page:{browser}:{url}"


def get_cached_page(url: str, browser: str) -> tuple[dict, bytes] | None:
    """The fetched page, shared by HTML and JSON mode, as (fields, raw HTML), or None.
    Stored as one JSON header line with the fields, followed by the raw HTML."""
    value = _get(page_key(url, browser))
    if value is None:
        return None
    try:
        header, content = zlib.decompress(value).split(b"\n", 1)
        fields = json.loads(header)
        if not isinstance(fields, dict):
            raise ValueError("header is not an object")
        return fields, content
    except (zlib.error, ValueError) as e:
        logger.warning("Ignoring a broken page cache entry for %s: %s", url, e)
        return None


def set_cached_page(url: str, browser: str, fields: dict, content: bytes, ok: bool) -> None:
    header = json.dumps(fields).encode("utf-8")
    _set(page_key(url, browser), zlib.compress(header + b"\n" + content, 6), _ttl(ok))


# ---- Small JSON records --------------------------------------------------------------------
# Per-site profiles and per-key cookies: small, so stored as plain JSON without compression.


def redis_available() -> bool:
    """True when Redis is configured and not paused, so "no record" from it means there is none."""
    return _redis() is not None


def get_json(name: str) -> dict | None:
    value = _get(f"{KEY_PREFIX}:{name}")
    if value is None:
        return None
    try:
        data = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        logger.warning("Ignoring a broken record at %s: %s", name, e)
        return None
    return data if isinstance(data, dict) else None


def set_json(name: str, value: dict, ttl: int) -> None:
    _set(f"{KEY_PREFIX}:{name}", json.dumps(value, separators=(",", ":")).encode("utf-8"), ttl)


# ---- Flags ---------------------------------------------------------------------------------
# Kept in Redis so every machine shares them, and in memory so they still work without Redis.

_flags: dict[str, float] = {}
_flags_lock = threading.Lock()


def raise_flag(name: str, ttl: int) -> None:
    with _flags_lock:
        now = time.monotonic()
        for stale in [k for k, until in _flags.items() if until <= now]:
            del _flags[stale]
        _flags[name] = now + ttl
    _set(f"{KEY_PREFIX}:flag:{name}", b"1", ttl)


def flag_is_up(name: str) -> bool:
    with _flags_lock:
        if _flags.get(name, 0.0) > time.monotonic():
            return True
    return _get(f"{KEY_PREFIX}:flag:{name}") is not None


def lower_flag(name: str) -> None:
    with _flags_lock:
        _flags.pop(name, None)
    client = _redis()
    if client is None:
        return
    try:
        client.delete(f"{KEY_PREFIX}:flag:{name}")
    except RedisError as e:
        _failed("delete", e)
