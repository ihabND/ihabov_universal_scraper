"""The API end to end, with fetching replaced by a fake so no real site is contacted."""

import pytest
from limits import parse_many

from scraper import quota, service
from scraper.page import Page

KEY = {"X-API-Key": "test-key"}  # one of the keys set in conftest.py

QUOTES = b'<div class="quote"><span class="text">Hi</span><small class="author">Ann</small></div>'


@pytest.fixture
def fetched(monkeypatch):
    """Every fetch returns a small page; the list records which URLs were fetched."""
    urls = []

    def fake_fetch(url, *args, **kwargs):
        urls.append(url)
        return Page(url, 200, QUOTES)

    monkeypatch.setattr(service, "fetch", fake_fetch)
    return urls


def test_health_needs_no_key(client):
    assert client.get("/health").json() == {"status": "ok", "redis": "off", "browser": "on", "proxy": "off"}


def test_wrong_keys_are_rejected_and_limited_per_ip(client):
    responses = [
        client.get("/scrape", params={"url": "https://example.com/"}, headers={"X-API-Key": "nope"}) for _ in range(11)
    ]
    assert [r.status_code for r in responses] == [401] * 10 + [429]
    assert responses[-1].json() == {"error": "Rate limit exceeded: 10 per 1 minute"}
    assert responses[-1].headers["retry-after"].isdigit()


def test_scrape_html(client, fetched):
    body = client.get("/scrape", params={"url": "https://example.com/page"}, headers=KEY).json()
    assert (body["url"], body["status_code"], body["ok"], body["fetched_with"]) == (
        "https://example.com/page",
        200,
        True,
        "http",
    )
    assert "Ann" in body["content"] and body["parsed"] is None


def test_scrape_json(client, fetched):
    body = client.get("/scrape", params={"url": "https://quotes.toscrape.com/", "mode": "json"}, headers=KEY).json()
    assert body["parsed"] == [{"quote": "Hi", "author": "Ann", "tags": []}]


def test_json_for_an_unsupported_site_is_400_without_fetching(client, fetched):
    response = client.get("/scrape", params={"url": "https://example.com/", "mode": "json"}, headers=KEY)
    assert response.status_code == 400 and fetched == []


def test_internal_addresses_are_refused(client):
    assert client.get("/scrape", params={"url": "http://127.0.0.1:6379/"}, headers=KEY).status_code == 403


def test_batch_keeps_order_reports_errors_per_url_and_fetches_duplicates_once(client, fetched):
    urls = ["https://example.com/a", "ftp://example.com/", "https://example.com/a"]
    body = client.post("/scrape/batch", json={"urls": urls}, headers=KEY).json()
    assert [r["url"] for r in body["results"]] == urls
    assert [r["status"] for r in body["results"]] == [200, 400, 200]
    assert (body["succeeded"], body["failed"]) == (2, 1)
    assert fetched == ["https://example.com/a"]


def test_batches_are_charged_per_url(client, fetched, monkeypatch):
    monkeypatch.setattr(quota, "KEY_LIMITS", parse_many("3/minute"))
    too_many = client.post("/scrape/batch", json={"urls": [f"https://example.com/{i}" for i in range(4)]}, headers=KEY)
    assert too_many.status_code == 429 and fetched == []  # refused whole, nothing fetched or charged
    assert client.post("/scrape/batch", json={"urls": ["https://example.com/1"] * 3}, headers=KEY).status_code == 200
    assert client.get("/scrape", params={"url": "https://example.com/1"}, headers=KEY).status_code == 429


def test_pages_render(client):
    home = client.get("/")
    assert home.status_code == 200
    assert all(f'data-host="{host}"' in home.text for host in ("books.toscrape.com", "imdb.com", "knjizare-vulkan.rs"))
    assert client.get("/docs").status_code == 200
    # static files carry a content fingerprint, so browsers never keep an old stylesheet
    assert "/static/app.css?v=" in home.text and "/static/app.js?v=" in home.text
