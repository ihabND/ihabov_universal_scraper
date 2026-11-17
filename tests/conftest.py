"""Shared test setup. Tests run offline: no real sites, no Redis server, no browser."""

import os

# Settings are read when the modules are imported, so they are set before anything from scraper loads.
os.environ["API_KEYS"] = "test-key,other-key"
os.environ.pop("REDIS_URL", None)  # tests that need Redis bring an in-memory fake
os.environ.pop("PROXY_URL", None)  # tests never go out through a real proxy

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from limits.storage import MemoryStorage  # noqa: E402
from limits.strategies import FixedWindowRateLimiter  # noqa: E402

from scraper import blocking, cache, profiles, quota  # noqa: E402
from scraper.app import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Every test starts with fresh rate-limit counters, no remembered sites and no saved cookies."""
    monkeypatch.setattr(quota, "_memory", FixedWindowRateLimiter(MemoryStorage()))
    blocking.clearances.clear()
    cache._flags.clear()
    profiles.clear_memory()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def redis(monkeypatch):
    """An in-memory fake Redis in place of the real one, for tests of what is stored there."""
    server = fakeredis.FakeServer()
    monkeypatch.setattr(cache, "redis_client", fakeredis.FakeRedis(server=server))
    monkeypatch.setattr(cache, "_paused_until", 0.0)
    return server
