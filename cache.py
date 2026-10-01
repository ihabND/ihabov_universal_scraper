import logging
import os

import redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600        # successful pages: 10 minutes
FAILED_CACHE_TTL_SECONDS = 30  # pages that returned 4xx/5xx: 30 seconds

REDIS_URL = os.environ.get("REDIS_URL")

# decode_responses=True gives back str instead of bytes.
# Short timeouts: if Redis is slow or down, skip the cache instead of hanging the request.
redis_client = (
    redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_timeout=1,
        socket_connect_timeout=1,
    )
    if REDIS_URL
    else None
)
if redis_client is None:
    logger.warning("REDIS_URL is not set, caching is disabled.")


def cache_key(url: str, mode: str, browser: str) -> str:
    # The browser option is part of the key: a page that came back blocked with browser=never
    # must not be served to a browser=auto request, and browser=always must not get an http result.
    return f"scrape:{mode}:{browser}:{url}"


def get_cached(url: str, mode: str, browser: str) -> str | None:
    if redis_client is None:
        return None
    try:
        return redis_client.get(cache_key(url, mode, browser))
    except RedisError as e:
        logger.warning("Redis read failed: %s", e)
        return None


def set_cached(url: str, mode: str, browser: str, value: str, ok: bool) -> None:
    if redis_client is None:
        return
    ttl = CACHE_TTL_SECONDS if ok else FAILED_CACHE_TTL_SECONDS
    try:
        redis_client.set(cache_key(url, mode, browser), value, ex=ttl)
    except RedisError as e:
        logger.warning("Redis write failed: %s", e)