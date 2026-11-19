"""Rate limits: per client IP for missing or wrong keys, and a quota per key charged per URL."""

import time

from limits import RateLimitItem, parse_many
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import FixedWindowRateLimiter
from limits.storage import storage_from_string

from ..settings import settings
from . import cache

IP_LIMITS = parse_many(settings.ip_rate_limit)
KEY_LIMITS = parse_many(settings.key_rate_limits)
TEST_IP_LIMITS = parse_many(settings.test_key_limits)
TEST_TOTAL_LIMITS = parse_many(settings.test_key_total_limits)

limiter = FixedWindowRateLimiter(
    storage_from_string(
        f"async+{settings.redis_url}", implementation="redispy", socket_timeout=1, socket_connect_timeout=1
    )
    if settings.redis_url
    else MemoryStorage()
)


async def take(
    limits: list[RateLimitItem], *ids: str, cost: int = 1, fail_open: bool | None = None
) -> tuple[str, float] | None:
    """Charge `cost` against every limit. None when allowed, otherwise the limit and when its window resets.
    `fail_open` says what an unreachable store means; RATE_LIMIT_FAIL_OPEN decides when it's None."""
    if fail_open is None:
        fail_open = settings.rate_limit_fail_open
    if settings.redis_url and not cache.available():
        return unavailable(fail_open)
    # One retry: the store's first call on a connection that is still opening (after a cold start) can time out.
    for attempt in (1, 2):
        try:
            # Check every limit before charging any, so a refused request uses up nothing.
            for item in limits:
                if not await limiter.test(item, *ids, cost=cost):
                    return str(item), (await limiter.get_window_stats(item, *ids)).reset_time
            for item in limits:
                await limiter.hit(item, *ids, cost=cost)
            return None
        except Exception as e:  # the storage is unreachable, whatever the client library raises
            if attempt == 2:
                cache.pause("rate limiting", e)
    return unavailable(fail_open)


async def warm() -> None:
    """Open the rate-limit store's own connection at startup, so the first request doesn't time out on it."""
    try:
        await limiter.storage.check()
    except Exception:  # an unreachable store is handled per request; startup must not fail over it
        pass


def unavailable(fail_open: bool) -> tuple[str, float] | None:
    return None if fail_open else ("rate limits are unavailable", time.time() + cache.PAUSE_SECONDS)
