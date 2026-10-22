"""The API end to end, with fetching replaced by a fake so no real site is contacted."""

from limits import parse_many

from scraper import quota
from scraper.app import SITE_CARDS


def test_health_needs_no_key(client):
    assert client.get("/health").json() == {"status": "ok", "redis": "off", "browser": "on"}


def test_wrong_keys_are_rejected_and_limited_per_ip(client):
    responses = [
        client.get("/scrape", params={"url": "https://example.com/"}, headers={"X-API-Key": "nope"}) for _ in range(11)
    ]
    assert [r.status_code for r in responses] == [401] * 10 + [429]
    assert responses[-1].json() == {"error": "Rate limit exceeded: 10 per 1 minute"}
    assert responses[-1].headers["retry-after"].isdigit()


def test_scrape_html(client, fetched, key):
    body = client.get("/scrape", params={"url": "https://example.com/page"}, headers=key).json()
    assert (body["url"], body["status_code"], body["ok"], body["fetched_with"]) == (
        "https://example.com/page",
        200,
        True,
        "http",
    )
    assert "Ann" in body["content"] and body["parsed"] is None


def test_scrape_json(client, fetched, key):
    body = client.get("/scrape", params={"url": "https://quotes.toscrape.com/", "mode": "json"}, headers=key).json()
    assert body["parsed"] == [{"quote": "Hi", "author": "Ann", "tags": []}]


def test_json_for_an_unsupported_site_is_400_without_fetching(client, fetched, key):
    response = client.get("/scrape", params={"url": "https://example.com/", "mode": "json"}, headers=key)
    assert response.status_code == 400 and fetched == []


def test_internal_addresses_are_refused(client, key):
    assert client.get("/scrape", params={"url": "http://127.0.0.1:6379/"}, headers=key).status_code == 403


def test_batch_keeps_order_reports_errors_per_url_and_fetches_duplicates_once(client, fetched, key):
    urls = ["https://example.com/a", "ftp://example.com/", "https://example.com/a"]
    body = client.post("/scrape/batch", json={"urls": urls}, headers=key).json()
    assert [r["url"] for r in body["results"]] == urls
    assert [r["status"] for r in body["results"]] == [200, 400, 200]
    assert (body["succeeded"], body["failed"]) == (2, 1)
    assert fetched == ["https://example.com/a"]


def test_batches_are_charged_per_url(client, fetched, key, monkeypatch):
    monkeypatch.setattr(quota, "USER_LIMITS", parse_many("3/minute"))
    too_many = client.post("/scrape/batch", json={"urls": [f"https://example.com/{i}" for i in range(4)]}, headers=key)
    assert too_many.status_code == 429 and fetched == []  # refused whole, nothing fetched or charged
    assert client.post("/scrape/batch", json={"urls": ["https://example.com/1"] * 3}, headers=key).status_code == 200
    assert client.get("/scrape", params={"url": "https://example.com/1"}, headers=key).status_code == 429


def test_pages_render(client):
    home = client.get("/")
    assert home.status_code == 200
    assert all(f'data-host="{host}"' in home.text for host in ("books.toscrape.com", "imdb.com", "knjizare-vulkan.rs"))
    assert client.get("/docs").status_code == 200
    # static files carry a content fingerprint, so browsers never keep an old stylesheet
    assert "/static/app.css?v=" in home.text and "/static/app.js?v=" in home.text


def test_docs_page_has_every_section_and_the_real_json_examples(client):
    page = client.get("/docs").text
    for section in ("quickstart", "authentication", "scrape", "response", "batch", "json", "errors", "rate-limits"):
        assert f'id="{section}"' in page and (section == "response" or f'href="#{section}"' in page)
    assert page.count('<details class="site-ex"') == len(SITE_CARDS)
    assert "10 requests a minute" in page and "How fetching works" in page
    assert page.count("data-tabs") == 3  # quickstart, /scrape and batch code cards
