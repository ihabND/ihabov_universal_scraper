"""Shared test setup. Tests run offline: no real sites, no Redis server, no browser."""

import os

# Settings are read when the modules are imported, so they are set before anything from scraper loads.
os.environ.pop("REDIS_URL", None)  # tests that need Redis bring an in-memory fake

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from limits.storage import MemoryStorage  # noqa: E402
from limits.strategies import MovingWindowRateLimiter  # noqa: E402

from scraper import auth, blocking, cache, db, quota, service, users  # noqa: E402
from scraper.app import app  # noqa: E402
from scraper.page import Page  # noqa: E402

PASSWORD = "correct horse battery"
QUOTES = b'<div class="quote"><span class="text">Hi</span><small class="author">Ann</small></div>'


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Every test starts with an empty database, fresh rate-limit counters, no remembered sites and
    no saved cookies."""
    monkeypatch.setattr(db, "PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(quota, "_memory", MovingWindowRateLimiter(MemoryStorage()))
    blocking.clearances.clear()
    cache._flags.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def user():
    return users.create_user("ann@example.com", PASSWORD)


@pytest.fixture
def key(user):
    """The X-API-Key header for a new key of `user`."""
    plain, _ = auth.create_key(user.id, "test")
    return {"X-API-Key": plain}


@pytest.fixture
def fetched(monkeypatch):
    """Every fetch returns a small page; the list records which URLs were fetched."""
    urls = []

    def fake_fetch(url, *args, **kwargs):
        urls.append(url)
        return Page(url, 200, QUOTES)

    monkeypatch.setattr(service, "fetch", fake_fetch)
    return urls
