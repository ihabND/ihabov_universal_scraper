"""Tests run offline: no real sites, no Redis server, no browser."""

import asyncio
import os

# Settings are read once on import, so they're set before anything from scraper loads.
os.environ["API_KEYS"] = "test-key,other-key"
for name in ("REDIS_URL", "PROXY_URL", "PROXY_BYPASS", "FLY_APP_NAME", "TRUSTED_IP_HEADER"):
    os.environ.pop(name, None)
os.environ["BROWSER_WARM"] = "off"

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from limits.aio.storage import MemoryStorage  # noqa: E402
from limits.aio.strategies import FixedWindowRateLimiter  # noqa: E402

from scraper.store import cache, quota  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    cache.memory.clear()
    monkeypatch.setattr(quota, "limiter", FixedWindowRateLimiter(MemoryStorage()))


@pytest.fixture
def redis(monkeypatch):
    """An in-memory fake Redis in place of the real one."""
    server = fakeredis.FakeServer()
    monkeypatch.setattr(cache, "client", fakeredis.FakeAsyncRedis(server=server))
    monkeypatch.setattr(cache, "paused_until", 0.0)
    return server


@pytest.fixture
async def ignore_socket_resets():
    """Windows' event loop reports a socket Chromium reset as an error in its own cleanup (a CPython bug)."""
    loop = asyncio.get_running_loop()
    report = loop.get_exception_handler() or (lambda loop, context: loop.default_exception_handler(context))
    loop.set_exception_handler(
        lambda loop, context: isinstance(context.get("exception"), ConnectionResetError) or report(loop, context)
    )
