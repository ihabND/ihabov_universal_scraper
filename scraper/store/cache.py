"""Redis, best effort: if it's missing or down, the scraper keeps working without it.

Pages are cached in Redis only. Small JSON records (bot-check cookies) fall back to
this process's memory while Redis is off or paused.
"""

import asyncio
import json
import logging
import time
import zlib

import redis.asyncio as redis
from redis.exceptions import RedisError

from ..core.page import Page
from ..settings import settings

KEY_PREFIX = "scrape:v3"  # bump when a stored format changes; old entries are then never read
PAGE_SECONDS = 600
FAILED_PAGE_SECONDS = 30
MAX_CACHED_BYTES = 4 * 1024 * 1024  # compressed
PAUSE_SECONDS = 30  # after an error, skip Redis this long instead of timing out on every call
MAX_MEMORY_RECORDS = 10_000

logger = logging.getLogger(__name__)
client = (
    redis.Redis.from_url(settings.redis_url, socket_timeout=1, socket_connect_timeout=1) if settings.redis_url else None
)
paused_until = 0.0
memory: dict[str, tuple[float, dict]] = {}  # name -> (expires at, record)


def available() -> bool:
    return client is not None and time.monotonic() >= paused_until


def pause(what: str, error: Exception) -> None:
    """After any Redis failure, every user of Redis (cache, records, rate limits) skips it for a while."""
    global paused_until
    paused_until = time.monotonic() + PAUSE_SECONDS
    logger.warning("Redis %s failed, skipping Redis for %d s: %s", what, PAUSE_SECONDS, error)


async def call(command: str, *args, **kwargs):
    try:
        return await getattr(client, command)(*args, **kwargs)
    except RedisError as e:
        pause(command, e)
        return None


async def close() -> None:
    if client is not None:
        await client.aclose()


async def redis_status() -> str:
    if client is None:
        return "off"
    if not available():
        return "paused"
    return "ok" if await call("ping") else "paused"


def page_key(url: str, browser: str) -> str:
    return f"{KEY_PREFIX}:page:{browser}:{url}"


async def get_page(url: str, browser: str) -> Page | None:
    value = await call("get", page_key(url, browser)) if available() else None
    if value is None:
        return None
    try:
        header, content = (await asyncio.to_thread(zlib.decompress, value)).split(b"\n", 1)
        return Page(content=content, **json.loads(header))
    except (zlib.error, ValueError, TypeError):
        return None


async def set_page(url: str, browser: str, page: Page) -> None:
    if not available():
        return
    header = {
        "url": page.url,
        "status_code": page.status_code,
        "fetched_with": page.fetched_with,
        "complete": page.complete,
    }
    value = await asyncio.to_thread(zlib.compress, json.dumps(header).encode() + b"\n" + page.content, 6)
    if len(value) <= MAX_CACHED_BYTES:
        await call("set", page_key(url, browser), value, ex=PAGE_SECONDS if page.ok else FAILED_PAGE_SECONDS)


async def get_json(name: str) -> dict | None:
    if not available():
        expires, record = memory.get(name, (0.0, None))
        return record if expires > time.time() else None
    value = await call("get", f"{KEY_PREFIX}:{name}")
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


async def set_json(name: str, record: dict, ttl: int) -> None:
    if available():
        await call("set", f"{KEY_PREFIX}:{name}", json.dumps(record, separators=(",", ":")), ex=ttl)
        return
    memory.pop(name, None)  # re-inserted at the end, so the first entry is always the oldest write
    memory[name] = (time.time() + ttl, record)
    if len(memory) > MAX_MEMORY_RECORDS:
        memory.pop(next(iter(memory)))
