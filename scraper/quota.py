"""Rate limits and quotas.

- Requests without a valid API key or login: limited per client IP, so guessing keys stays slow.
- Requests from a user: one quota per user, shared by all of their keys and the website, charged
  per URL. A batch of 5 URLs costs 5.
- Logins and sign-ups: limited per IP (and logins per email too), so passwords can't be guessed
  quickly and accounts can't be made in bulk to get around the user quota.

Limits use a moving window: "10/minute" means at most 10 in any 60 seconds, not 10 per clock minute,
which would allow 20 within a few seconds around the turn of the minute.

Counters live in Redis when REDIS_URL is set, so every machine shares them. If Redis is missing or
fails, the counters fall back to this process's memory.
"""

import logging
import os
import time
from typing import NamedTuple

from limits import RateLimitItem, parse_many
from limits.storage import MemoryStorage, storage_from_string
from limits.strategies import MovingWindowRateLimiter

logger = logging.getLogger(__name__)

IP_LIMITS = parse_many(os.environ.get("IP_RATE_LIMIT", "10/minute"))
USER_LIMITS = parse_many(os.environ.get("USER_RATE_LIMIT", "10/minute"))
LOGIN_LIMITS = parse_many(os.environ.get("LOGIN_RATE_LIMIT", "5/minute"))
SIGNUP_LIMITS = parse_many(os.environ.get("SIGNUP_RATE_LIMIT", "3/day"))
REDIS_PAUSE_SECONDS = 30

_memory = MovingWindowRateLimiter(MemoryStorage())
_redis_url = os.environ.get("REDIS_URL")
_shared = (
    MovingWindowRateLimiter(storage_from_string(_redis_url, socket_timeout=1, socket_connect_timeout=1))
    if _redis_url
    else None
)
_paused_until = 0.0


class Usage(NamedTuple):
    limit: RateLimitItem
    remaining: int
    reset_at: float  # time.time() when room frees up again


def _limiter_call(call):
    """Run `call(limiter)` against Redis when it's available, otherwise against memory."""
    global _paused_until
    if _shared is not None and time.monotonic() >= _paused_until:
        try:
            return call(_shared)
        except Exception as e:  # any storage failure: count in memory instead of failing the request
            _paused_until = time.monotonic() + REDIS_PAUSE_SECONDS
            logger.warning("Rate limit storage unreachable, counting in memory for %d s: %s", REDIS_PAUSE_SECONDS, e)
    return call(_memory)


def _charge(
    limiter: MovingWindowRateLimiter, limits: list[RateLimitItem], ids: tuple[str, ...], cost: int
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
    exceeded and the time.time() at which there is room again."""
    return _limiter_call(lambda limiter: _charge(limiter, limits, ids, cost))


def peek(limits: list[RateLimitItem], *ids: str) -> Usage:
    """How much is left of the tightest limit, without charging anything."""

    def stats(limiter: MovingWindowRateLimiter) -> Usage:
        usages = []
        for item in limits:
            window = limiter.get_window_stats(item, *ids)
            usages.append(Usage(item, window.remaining, window.reset_time))
        return min(usages, key=lambda u: u.remaining)

    return _limiter_call(stats)


def describe(limits: list[RateLimitItem]) -> str:
    """For people: "10 requests a minute", or "10 requests per 2 minutes and 500 a day"."""
    parts = []
    for item in limits:
        unit = item.GRANULARITY.name
        per = f"a {unit}" if item.multiples == 1 else f"per {item.multiples} {unit}s"
        parts.append(f"{item.amount} {per}")
    return parts[0].replace(" ", " requests ", 1) + "".join(f" and {part}" for part in parts[1:])


def take_ip(ip: str) -> tuple[RateLimitItem, float] | None:
    return take(IP_LIMITS, "ip", ip)


def take_user(user_id: int, cost: int) -> tuple[RateLimitItem, float] | None:
    return take(USER_LIMITS, "user", str(user_id), cost=cost)


def user_usage(user_id: int) -> Usage:
    return peek(USER_LIMITS, "user", str(user_id))


def take_login(ip: str, email: str) -> tuple[RateLimitItem, float] | None:
    # Per IP stops one machine trying many accounts; per email stops many machines trying one account.
    return take(LOGIN_LIMITS, "login-ip", ip) or take(LOGIN_LIMITS, "login-email", email)


def take_signup(ip: str) -> tuple[RateLimitItem, float] | None:
    return take(SIGNUP_LIMITS, "signup", ip)
