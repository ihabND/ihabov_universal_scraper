"""The Redis cache, against an in-memory fake Redis."""

import fakeredis
import pytest

from scraper import cache, service
from scraper.page import Page

URL = "https://quotes.toscrape.com/page/2/"
PAGE = b'<div class="quote"><span class="text">Hi</span><small class="author">Ann</small></div>'


@pytest.fixture
def redis(monkeypatch):
    server = fakeredis.FakeServer()
    monkeypatch.setattr(cache, "redis_client", fakeredis.FakeRedis(server=server))
    monkeypatch.setattr(cache, "_paused_until", 0.0)
    return server


@pytest.fixture
def fetched(monkeypatch):
    urls = []

    def fake_fetch(url, *args, **kwargs):
        urls.append(url)
        return Page(url, 200, PAGE)

    monkeypatch.setattr(service, "fetch", fake_fetch)
    return urls


def test_pages_are_stored_compressed_with_a_ttl(redis):
    content = b"<html>" + b"<p>same text</p>" * 1000 + b"</html>"
    cache.set_cached_page(URL, "auto", {"status_code": 200}, content, ok=True)
    assert cache.get_cached_page(URL, "auto") == ({"status_code": 200}, content)
    client = cache.redis_client
    key = cache.page_key(URL, "auto")
    assert 590 < client.ttl(key) <= 600
    assert len(client.get(key)) < len(content) / 10


def test_error_pages_expire_quickly(redis):
    cache.set_cached_page(URL, "auto", {"status_code": 404}, b"<html>gone</html>", ok=False)
    assert 0 < cache.redis_client.ttl(cache.page_key(URL, "auto")) <= 30


def test_json_and_html_share_one_fetch(redis, client, fetched, key):
    assert client.get("/scrape", params={"url": URL, "mode": "json"}, headers=key).status_code == 200
    assert client.get("/scrape", params={"url": URL, "mode": "html"}, headers=key).status_code == 200
    assert client.get("/scrape", params={"url": URL, "mode": "json"}, headers=key).status_code == 200
    assert fetched == [URL]


def test_a_cut_short_browser_page_is_not_reused_for_html(redis):
    service.remember_page(URL, "auto", Page(URL, 200, PAGE, "browser", complete=False))
    assert service.cached_page(URL, "auto", "json") is not None
    assert service.cached_page(URL, "auto", "html") is None


def test_scraping_still_works_when_redis_is_down(redis, client, fetched, key):
    redis.connected = False
    assert client.get("/scrape", params={"url": URL}, headers=key).status_code == 200
    assert client.get("/health").json()["redis"] == "paused"
