import time

import pytest
from limits import parse_many

from scraper.core.page import Page
from scraper.store import cache, quota, strategies

pytestmark = pytest.mark.anyio

URL = "https://quotes.toscrape.com/page/2/"


async def test_pages_are_stored_compressed_with_a_ttl(redis):
    page = Page(URL, 200, b"<html>" + b"<p>same text</p>" * 1000 + b"</html>", "browser", complete=False)
    await cache.set_page(URL, "auto", page)
    assert await cache.get_page(URL, "auto") == page
    key = cache.page_key(URL, "auto")
    assert 590 < await cache.client.ttl(key) <= 600
    assert len(await cache.client.get(key)) < len(page.content) / 10


async def test_error_pages_expire_after_30_seconds(redis):
    await cache.set_page(URL, "auto", Page(URL, 404, b"<html>gone</html>"))
    assert 0 < await cache.client.ttl(cache.page_key(URL, "auto")) <= 30


async def test_records_live_in_memory_without_redis():
    await cache.set_json("site:shop.example", {"tier": "browser"}, 600)
    assert await cache.get_json("site:shop.example") == {"tier": "browser"}
    assert await cache.get_page(URL, "auto") is None


async def test_records_go_to_memory_while_redis_is_paused(redis, monkeypatch):
    monkeypatch.setattr(cache, "paused_until", time.monotonic() + 30)
    await cache.set_json("site:shop.example", {"tier": "browser"}, 600)
    assert await cache.get_json("site:shop.example") == {"tier": "browser"}


async def test_a_redis_error_pauses_redis(redis):
    redis.connected = False
    assert await cache.get_json("site:shop.example") is None
    assert await cache.redis_status() == "paused"


async def test_wrong_keys_are_limited_per_ip():
    results = [await quota.take(quota.IP_LIMITS, "ip", "1.2.3.4") for _ in range(11)]
    assert results[:10] == [None] * 10
    limit, reset_at = results[10]
    assert limit == "10 per 1 minute" and reset_at > time.time()


async def test_a_charge_that_does_not_fit_is_refused_whole(monkeypatch):
    limits = parse_many("3/minute")
    assert await quota.take(limits, "key", "k", cost=4) is not None
    assert await quota.take(limits, "key", "k", cost=3) is None  # nothing was used by the refused charge
    assert await quota.take(limits, "key", "k") is not None


class BrokenLimiter:
    def __init__(self):
        self.calls = 0

    async def test(self, *args, **kwargs):
        self.calls += 1
        raise ConnectionError("Redis is down")


async def test_a_rate_limit_store_failure_pauses_redis_and_later_requests_skip_it(monkeypatch):
    monkeypatch.setattr(quota.settings, "redis_url", "redis://localhost:1")
    monkeypatch.setattr(cache, "client", object())  # configured, so "available" depends on the pause
    monkeypatch.setattr(cache, "paused_until", 0.0)
    limiter = BrokenLimiter()
    monkeypatch.setattr(quota, "limiter", limiter)
    assert await quota.take(quota.KEY_LIMITS, "key", "k") is None  # fails open by default
    assert await quota.take(quota.KEY_LIMITS, "key", "k") is None
    assert limiter.calls == 1 and not cache.available()


def test_pins_beat_learned_values_which_beat_defaults_and_everything_survives_reopening():
    strategies.learn("shop.example", "start", "browser", "plain HTTP got 403 (cloudflare)")
    strategies.set_pins("shop.example", {"start": "http", "render_wait": 8})
    strategies.learn("shop.example", "start", "browser", "plain HTTP got 403 again")  # disagrees with the pin
    strategies.close()
    record = strategies.get("shop.example")
    assert record.site == "shop.example" and record.learned["start"]["value"] == "browser"
    assert record.flags["start"]["seen"] == "plain HTTP got 403 again"
    strategy = strategies.strategy_for("shop.example", seed_start="http")
    assert (strategy.start, strategy.render_wait, strategy.retries) == ("http", 8, 2)
