import asyncio
import time
from itertools import pairwise

import pytest

from scraper.antibot.clearance import Clearance, save_clearance
from scraper.core.errors import BrowserDisabledError, UnsupportedSiteError
from scraper.core.page import Page
from scraper.core.service import Scraper, SiteGate
from scraper.net.guard import EgressGuard
from scraper.store import cache

pytestmark = pytest.mark.anyio

QUOTES = "https://quotes.toscrape.com/page/2/"
QUOTE_HTML = b'<div class="quote"><span class="text">Hi</span><small class="author">Ann</small></div>'


class SlowHttp:
    """Answers every URL with the quotes page after `delay`, and tracks how many fetches overlap."""

    name, renders, status = "http", False, "ready"

    def __init__(self, delay: float = 0.0):
        self.delay, self.urls, self.running, self.most_at_once, self.cookies = delay, [], 0, 0, []

    async def fetch(self, req):
        self.urls.append(req.url)
        self.cookies.append(req.clearance)
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        await asyncio.sleep(self.delay)
        self.running -= 1
        return Page(req.url, 200, QUOTE_HTML)


@pytest.fixture
def http():
    return SlowHttp()


@pytest.fixture
def scraper(http):
    return Scraper([http], EgressGuard())


async def test_html_and_json_share_one_fetch(scraper, http, redis):
    assert (await scraper.scrape(QUOTES, "json")).parsed == [{"quote": "Hi", "author": "Ann", "tags": []}]
    assert "Ann" in (await scraper.scrape(QUOTES, "html")).content
    await scraper.scrape(QUOTES, "json")
    assert http.urls == [QUOTES]


async def test_a_cut_short_browser_page_is_not_reused_for_html(scraper, http, redis):
    await cache.set_page(QUOTES, "auto", Page(QUOTES, 200, QUOTE_HTML, "browser", complete=False))
    await scraper.scrape(QUOTES, "json")
    assert http.urls == []
    await scraper.scrape(QUOTES, "html")
    assert http.urls == [QUOTES]


async def test_identical_requests_at_the_same_time_fetch_once(scraper, http):
    http.delay = 0.05
    results = await asyncio.gather(*(scraper.scrape(QUOTES) for _ in range(45)))
    assert http.urls == [QUOTES] and len({r.content for r in results}) == 1


async def test_a_cancelled_caller_does_not_cancel_the_fetch_for_the_others(scraper, http):
    http.delay = 0.05
    first = asyncio.create_task(scraper.scrape(QUOTES))
    second = asyncio.create_task(scraper.scrape(QUOTES))
    await asyncio.sleep(0.01)
    first.cancel()
    assert (await second).ok and http.urls == [QUOTES]


async def test_json_for_an_unsupported_site_is_refused_before_fetching(scraper, http):
    with pytest.raises(UnsupportedSiteError):
        await scraper.scrape("https://example.com/", "json")
    assert http.urls == []


async def test_browser_always_without_a_browser_is_refused(scraper):
    with pytest.raises(BrowserDisabledError):
        await scraper.scrape(QUOTES, browser="always")


async def test_a_batch_keeps_order_reports_errors_per_url_and_fetches_duplicates_once(scraper, http):
    urls = ["https://example.com/a", "ftp://example.com/", "https://Example.com/a"]
    batch = await scraper.batch(urls, "html", "auto", owner="alice")
    assert [r.url for r in batch.results] == urls
    assert [r.status for r in batch.results] == [200, 400, 200]
    assert (batch.succeeded, batch.failed) == (2, 1)
    assert http.urls == ["https://example.com/a"]


async def test_one_key_s_batch_runs_a_limited_number_of_urls_at_once(scraper, http, monkeypatch):
    monkeypatch.setattr("scraper.core.service.settings.batch_workers", 2)
    http.delay = 0.02
    await Scraper([http], EgressGuard()).batch([f"https://site{i}.example/" for i in range(6)], "html", "auto", "alice")
    assert http.most_at_once == 2


async def test_the_site_gate_spaces_out_requests_to_one_site_and_forgets_it_after():
    gate, starts = SiteGate(per_site=4, gap=0.05), []

    async def visit():
        async with gate("https://shop.example/a"):
            starts.append(time.monotonic())

    await asyncio.gather(visit(), visit(), visit())
    assert all(b - a >= 0.04 for a, b in pairwise(starts))
    assert gate.sites == {}


async def test_a_key_s_cookies_never_ride_along_on_another_key_s_shared_or_batched_scrape(scraper, http):
    await save_clearance("alice", "quotes.toscrape.com", Clearance([{"name": "c", "value": "x"}], {}))
    http.delay = 0.05
    await asyncio.gather(scraper.scrape(QUOTES, owner="bob"), scraper.scrape(QUOTES, owner="alice"))
    await scraper.batch(["https://quotes.toscrape.com/page/3/"], "html", "auto", owner="bob")
    assert http.cookies == [None, None]  # bob's scrape was shared with alice, never the other way round
