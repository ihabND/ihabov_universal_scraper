"""The app end to end. Fetching is a fake unless a test needs the real (offline) checks."""

import pytest
from fastapi.testclient import TestClient
from limits import parse_many

from scraper.app import app
from scraper.core.page import Page
from scraper.settings import settings
from scraper.store import quota

KEY = {"X-API-Key": "test-key"}
QUOTES = b'<div class="quote"><span class="text">Hi</span><small class="author">Ann</small></div>'


class FakeHttp:
    name, renders, status = "http", False, "ready"

    def __init__(self):
        self.urls = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def fetch(self, req):
        self.urls.append(req.url)
        return Page(req.url, 200, QUOTES)


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def fetched(client, monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(client.app.state.scraper, "tiers", [fake])
    return fake.urls


def scrape(client, url, **params):
    return client.get("/scrape", params={"url": url, **params}, headers=KEY)


def test_health_needs_no_key(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and set(body) == {"status", "redis", "browser", "proxy"}


def test_wrong_keys_are_limited_per_ip(client):
    responses = [
        client.get("/scrape", params={"url": "https://example.com/"}, headers={"X-API-Key": "nope"}) for _ in range(11)
    ]
    assert [r.status_code for r in responses] == [401] * 10 + [429]
    assert responses[-1].json() == {"detail": "Rate limit exceeded: 10 per 1 minute"}
    assert responses[-1].headers["retry-after"].isdigit()


def wrong_key_statuses(client, header: str) -> list[int]:
    return [
        client.get(
            "/scrape", params={"url": "https://e.com/"}, headers={"X-API-Key": "nope", header: f"10.0.0.{i}"}
        ).status_code
        for i in range(11)
    ]


def test_a_forwarded_ip_is_ignored_unless_that_header_is_trusted(client, monkeypatch):
    assert wrong_key_statuses(client, "fly-client-ip")[-1] == 429
    monkeypatch.setattr(settings, "trusted_ip_header", "x-real-ip")
    assert 429 not in wrong_key_statuses(client, "x-real-ip")


def test_scrape_html(client, fetched):
    body = scrape(client, "https://example.com/page").json()
    assert (body["url"], body["status_code"], body["ok"], body["fetched_with"]) == (
        "https://example.com/page",
        200,
        True,
        "http",
    )
    assert "Ann" in body["content"] and body["parsed"] is None


def test_scrape_json(client, fetched):
    body = scrape(client, "https://quotes.toscrape.com/", mode="json").json()
    assert body["parsed"] == [{"quote": "Hi", "author": "Ann", "tags": []}]


def test_json_on_a_page_without_structured_data_is_a_502(client, fetched):
    response = scrape(client, "https://example.com/", mode="json")
    assert response.status_code == 502 and "structured data" in response.json()["detail"]


def test_internal_addresses_are_refused(client):
    response = scrape(client, "http://127.0.0.1:6379/")
    assert response.status_code == 403 and response.json() == {"detail": "This host is not allowed"}


def test_a_batch_keeps_order_and_reports_errors_per_url(client, fetched):
    urls = ["https://example.com/a", "ftp://example.com/", "https://example.com/a"]
    body = client.post("/scrape/batch", json={"urls": urls}, headers=KEY).json()
    assert [r["status"] for r in body["results"]] == [200, 400, 200] and fetched == ["https://example.com/a"]


def test_batches_are_charged_per_url(client, fetched, monkeypatch):
    monkeypatch.setattr(quota, "KEY_LIMITS", parse_many("3/minute"))
    too_many = client.post("/scrape/batch", json={"urls": [f"https://example.com/{i}" for i in range(4)]}, headers=KEY)
    assert too_many.status_code == 429 and fetched == []  # refused whole, nothing fetched or charged
    assert client.post("/scrape/batch", json={"urls": ["https://example.com/1"] * 3}, headers=KEY).status_code == 200
    assert scrape(client, "https://example.com/1").status_code == 429


def test_pages_render_with_fingerprinted_assets(client):
    home = client.get("/")
    assert all(f'data-host="{host}"' in home.text for host in ("books.toscrape.com", "imdb.com", "knjizare-vulkan.rs"))
    assert "/static/app.css?v=" in home.text and "/static/app.js?v=" in home.text
    assert client.get("/docs").status_code == 200


def test_an_unknown_option_in_the_page_url_still_renders_the_page(client):
    assert client.get("/", params={"mode": "pdf", "browser": "sometimes"}).status_code == 200


def test_pages_send_security_headers(client):
    headers = client.get("/").headers
    assert "script-src 'self'" in headers["content-security-policy"] and headers["x-content-type-options"] == "nosniff"


def test_the_form_works_without_javascript(client, fetched):
    form = {"url": "https://quotes.toscrape.com/", "mode": "json", "browser": "auto"}
    page = client.post("/", data={**form, "api_key": "test-key"})
    assert page.status_code == 200 and 'id="seed"' in page.text and "Ann" in page.text
    refused = client.post("/", data={**form, "api_key": "wrong"})
    assert refused.status_code == 401 and "Invalid or missing API key" in refused.text


def test_the_front_page_lists_sites_in_registration_order():
    from scraper.web.views import SITE_CARDS

    assert [card["host"] for card in SITE_CARDS][:3] == ["webscraper.io", "scrapeme.live", "books.toscrape.com"]
    assert SITE_CARDS[2]["label"] == "Book listing or book page" and SITE_CARDS[-1]["host"] == "imdb.com"
