"""The Redis cache, against an in-memory fake Redis."""

import time

import pytest

from scraper import cache, service
from scraper.page import Page

KEY = {"X-API-Key": "test-key"}  # one of the keys set in conftest.py

URL = "https://quotes.toscrape.com/page/2/"
PAGE = b'<div class="quote"><span class="text">Hi</span><small class="author">Ann</small></div>'


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


def test_json_and_html_share_one_fetch(redis, client, fetched):
    assert client.get("/scrape", params={"url": URL, "mode": "json"}, headers=KEY).status_code == 200
    assert client.get("/scrape", params={"url": URL, "mode": "html"}, headers=KEY).status_code == 200
    assert client.get("/scrape", params={"url": URL, "mode": "json"}, headers=KEY).status_code == 200
    assert fetched == [URL]


def test_a_cut_short_browser_page_is_not_reused_for_html(redis):
    service.remember_page(URL, "auto", Page(URL, 200, PAGE, "browser", complete=False))
    assert service.cached_page(URL, "auto", "json") is not None
    assert service.cached_page(URL, "auto", "html") is None


def test_scraping_still_works_when_redis_is_down(redis, client, fetched):
    redis.connected = False
    assert client.get("/scrape", params={"url": URL}, headers=KEY).status_code == 200
    assert client.get("/health").json()["redis"] == "paused"


def test_json_values_round_trip_with_a_ttl(redis):
    cache.set_json("site:shop.example", {"b": True, "r": ["parse_movie"]}, 600)
    assert cache.get_json("site:shop.example") == {"b": True, "r": ["parse_movie"]}
    assert 0 < cache.redis_client.ttl("scrape:v2:site:shop.example") <= 600


def test_broken_json_values_read_as_missing(redis):
    cache.redis_client.set("scrape:v2:site:bad.example", bytes([0xFF]) + b" not json")
    cache.redis_client.set("scrape:v2:site:list.example", b"[1, 2]")
    assert cache.get_json("site:bad.example") is None
    assert cache.get_json("site:list.example") is None
    assert cache.get_json("site:missing.example") is None


def test_json_helpers_do_nothing_without_redis():
    cache.set_json("site:shop.example", {"b": True}, 600)  # no fixture: redis_client is None
    assert cache.get_json("site:shop.example") is None
    assert not cache.redis_available()


def test_redis_is_unavailable_while_paused(redis, monkeypatch):
    assert cache.redis_available()
    monkeypatch.setattr(cache, "_paused_until", time.monotonic() + 30)
    assert not cache.redis_available()
