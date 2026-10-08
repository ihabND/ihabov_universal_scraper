"""Rate limits and quotas.

- Requests without a valid API key: limited per client IP, so guessing keys stays slow.
- Requests with a valid key: a quota per key, charged per URL. A batch of 20 URLs costs 20.

Counters live in Redis when REDIS_URL is set, so every machine shares them. If Redis is missing or
fails, the counters fall back to this process's memory, as slowapi did before.
"""

import hashlib
import logging
import os
import time

from limits import RateLimitItem, parse_many
from limits.storage import MemoryStorage, storage_from_string
from limits.strategies import FixedWindowRateLimiter

logger = logging.getLogger(__name__)

IP_LIMITS = parse_many(os.environ.get("IP_RATE_LIMIT", "10/minute"))
KEY_LIMITS = parse_many(os.environ.get("KEY_RATE_LIMITS", "300/minute;10000/day"))
REDIS_PAUSE_SECONDS = 30

_memory = FixedWindowRateLimiter(MemoryStorage())
_redis_url = os.environ.get("REDIS_URL")
_shared = (
    FixedWindowRateLimiter(storage_from_string(_redis_url, socket_timeout=1, socket_connect_timeout=1))
    if _redis_url
    else None
)
_paused_until = 0.0


def key_id(api_key: str) -> str:
    """A stable, non-reversible name for a key, used for counters and per-key state.
    The key itself is never stored anywhere."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _charge(
    limiter: FixedWindowRateLimiter, limits: list[RateLimitItem], ids: tuple[str, ...], cost: int
) -> tuple[RateLimitItem, float] | None:
    # Check every limit first, then charge them all, so a refused request uses up nothing.
    for item in limits:
        if not limiter.test(item, *ids, cost=cost):
            return item, limiter.get_window_stats(item, *ids).reset_time
    for item in limits:
        limiter.hit(item, *ids, cost=cost)
    return None


def take(limits: list[RateLimitItem], *ids: str, cost: int = 1) -> tuple[RateLimitItem, float] | None:
    """Charge `cost` against every limit. Returns None if allowed, otherwise the limit that would be
    exceeded and the time.time() at which its window resets."""
    global _paused_until
    if _shared is not None and time.monotonic() >= _paused_until:
        try:
            return _charge(_shared, limits, ids, cost)
        except Exception as e:  # any storage failure: count in memory instead of failing the request
            _paused_until = time.monotonic() + REDIS_PAUSE_SECONDS
            logger.warning("Rate limit storage unreachable, counting in memory for %d s: %s", REDIS_PAUSE_SECONDS, e)
    return _charge(_memory, limits, ids, cost)


def take_ip(ip: str) -> tuple[RateLimitItem, float] | None:
    return take(IP_LIMITS, "ip", ip)


def take_key(api_key: str, cost: int) -> tuple[RateLimitItem, float] | None:
    return take(KEY_LIMITS, "key", key_id(api_key), cost=cost)
