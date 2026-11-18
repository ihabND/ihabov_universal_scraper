"""Which fetcher runs when, and what is remembered. The fetchers are fakes, so only the decisions run."""

import pytest

from scraper.antibot.clearance import Clearance, get_clearance, save_clearance
from scraper.core import ladder
from scraper.core.errors import BlockedError, BrowserBusyError, FetchError, ParseError
from scraper.core.page import Page
from scraper.parsers import find_parser
from scraper.store.profiles import SiteProfile, get_profile, save_profile

pytestmark = pytest.mark.anyio

URL = "https://shop.example/item"
OPEN = Page(URL, 200, b"<html>open</html>")
BLOCKED = Page(URL, 403, b"<title>Just a moment...</title>")
RENDERED = Page(URL, 200, b"<html>rendered</html>", "browser")
IMDB = "https://www.imdb.com/title/tt0111161/"
QUOTES = "https://quotes.toscrape.com/page/2/"
QUOTE_HTML = b'<div class="quote"><span class="text">Hi</span><small class="author">Ann</small></div>'
EMPTY = Page(QUOTES, 200, b"<html></html>")  # the plain page, before JavaScript adds the quotes
RENDERED_QUOTES = Page(QUOTES, 200, QUOTE_HTML, "browser")
COOKIE = {"name": "cf_clearance", "value": "x", "domain": ".shop.example", "path": "/"}


class Fake:
    status = "ready"

    def __init__(self, name: str, renders: bool, result):
        self.name, self.renders, self.result, self.requests = name, renders, result, []

    async def fetch(self, req):
        self.requests.append((req.clearance, req.after_block))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def http():
    return Fake("http", False, OPEN)


@pytest.fixture
def browser():
    return Fake("browser", True, RENDERED)


async def fetch(url, tiers, **options):
    page, parsed = await ladder.fetch(url, tiers, **options)
    return page if parsed is None else parsed


async def test_an_open_site_never_starts_the_browser(http, browser):
    assert await fetch(URL, [http, browser]) is OPEN
    assert browser.requests == []


async def test_a_blocked_site_goes_to_the_browser_and_is_remembered(http, browser):
    http.result = BLOCKED
    assert await fetch(URL, [http, browser]) is RENDERED
    assert await fetch(URL, [http, browser]) is RENDERED
    assert len(http.requests) == 1 and len(browser.requests) == 2  # the second time, no plain attempt wasted
    assert (await get_profile("shop.example")).tier == "browser"


async def test_a_site_that_blocks_one_fingerprint_is_remembered_on_the_other(http, browser):
    http.result = BLOCKED
    safari = Fake("http-safari", False, OPEN)
    assert await fetch(URL, [http, safari, browser]) is OPEN
    assert await fetch(URL, [http, safari, browser]) is OPEN
    assert len(http.requests) == 1 and len(safari.requests) == 2 and browser.requests == []
    assert (await get_profile("shop.example")).tier == "http-safari"


async def test_without_the_browser_a_blocked_page_is_returned(http):
    http.result = BLOCKED
    assert await fetch(URL, [http]) is BLOCKED


async def test_a_remembered_route_that_fails_still_gets_the_plain_page(http, browser):
    await save_profile("shop.example", SiteProfile("browser"))
    browser.result = FetchError("browser crashed")
    assert await fetch(URL, [http, browser]) is OPEN
    assert (await get_profile("shop.example")).tier == "http"


async def test_when_every_route_fails_the_remembered_route_s_error_stands(http, browser):
    await save_profile("shop.example", SiteProfile("browser"))
    browser.result, http.result = FetchError("browser crashed"), FetchError("site down")
    with pytest.raises(FetchError, match="browser crashed"):
        await fetch(URL, [http, browser])


async def test_a_server_error_the_browser_cannot_load_returns_the_plain_page(http, browser):
    http.result = Page(URL, 503, b"<html>down for maintenance</html>")
    browser.result = FetchError("net::ERR_HTTP_RESPONSE_CODE_FAILURE")
    assert await fetch(URL, [http, browser]) is http.result


async def test_a_site_down_for_both_is_not_learned_as_blocking(http, browser):
    http.result = Page(URL, 503, b"<html>down for maintenance</html>")
    browser.result = Page(URL, 503, b"<html>down for maintenance</html>", "browser")
    await fetch(URL, [http, browser])
    assert await get_profile("shop.example") is None


async def test_a_seeded_site_goes_to_the_browser_first(http, browser):
    await fetch(IMDB, [http, browser])
    assert http.requests == [] and len(browser.requests) == 1


async def test_saved_cookies_are_only_used_by_their_owner(http, browser):
    await save_clearance("alice", "shop.example", Clearance([COOKIE], {}))
    await fetch(URL, [http, browser], owner="alice")
    await fetch(URL, [http, browser], owner="bob")
    (alice, _), (bob, _) = http.requests
    assert alice.cookies == [COOKIE] and bob is None


async def test_cookies_beat_the_profile(http, browser):
    await save_profile("shop.example", SiteProfile("browser"))
    await save_clearance("alice", "shop.example", Clearance([COOKIE], {}))
    assert await fetch(URL, [http, browser], owner="alice") is OPEN
    assert browser.requests == [] and (await get_profile("shop.example")).tier == "browser"


async def test_plain_http_blocked_even_with_cookies_is_remembered_for_those_cookies(http, browser):
    await save_clearance("alice", "shop.example", Clearance([COOKIE], {}))
    http.result = BLOCKED
    await fetch(URL, [http, browser], owner="alice")
    assert not (await get_clearance("alice", "shop.example")).plain_ok


async def test_after_plain_http_is_blocked_with_saved_cookies_the_browser_starts_without_them(http, browser):
    await save_clearance("alice", "shop.example", Clearance([COOKIE], {}))
    http.result = BLOCKED
    await fetch(URL, [http, browser], owner="alice")
    await fetch(URL, [http, browser], owner="alice")  # the cookies are marked: plain is skipped, browser starts fresh
    assert browser.requests == [(None, True), (None, False)]


async def test_cookies_from_a_passed_bot_check_are_kept(http, browser):
    http.result = BLOCKED
    browser.result = Page(
        URL, 200, b"<html>rendered</html>", "browser", earned=Clearance([COOKIE], {"user-agent": "UA"})
    )
    await fetch(URL, [http, browser], owner="alice")
    assert browser.requests == [(None, True)]  # told it ran after a block
    assert (await get_clearance("alice", "shop.example")).headers == {"user-agent": "UA"}


async def test_a_blocked_browser_page_keeps_no_cookies(http, browser):
    http.result = BLOCKED
    browser.result = Page(URL, 429, b"<html>denied</html>", "browser", earned=Clearance([COOKIE], {}))
    page = await fetch(URL, [http, browser], owner="alice")
    assert page.status_code == 429 and not page.ok
    assert await get_clearance("alice", "shop.example") is None


# ---- JSON mode -------------------------------------------------------------------------------


async def test_json_mode_on_a_blocked_page_is_a_blocked_error_that_suggests_the_browser(http):
    http.result = Page(QUOTES, 403, b"<title>Just a moment...</title>")
    with pytest.raises(BlockedError, match="browser=auto"):
        await fetch(QUOTES, [http], parser=find_parser(QUOTES))


async def test_a_busy_browser_on_a_blocked_site_is_busy_not_blocked(http, browser):
    http.result, browser.result = BLOCKED, BrowserBusyError("busy")
    with pytest.raises(BrowserBusyError):
        await fetch(URL, [http, browser])


async def test_a_parse_that_needed_the_browser_is_remembered(http, browser):
    http.result, browser.result = EMPTY, RENDERED_QUOTES
    parsed = await fetch(QUOTES, [http, browser], parser=find_parser(QUOTES))
    assert parsed == [{"quote": "Hi", "author": "Ann", "tags": []}]
    assert (await get_profile("quotes.toscrape.com")).render == ["parse_quotes"]
    await fetch(QUOTES, [http, browser], parser=find_parser(QUOTES))
    assert len(http.requests) == 1 and len(browser.requests) == 2


async def test_html_mode_ignores_the_render_memory(http, browser):
    await save_profile("quotes.toscrape.com", SiteProfile(render=["parse_quotes"]))
    await fetch(QUOTES, [http, browser])
    assert len(http.requests) == 1 and browser.requests == []


async def test_without_the_browser_a_page_without_the_data_is_a_parse_error(http):
    await save_profile("quotes.toscrape.com", SiteProfile(render=["parse_quotes"]))
    http.result = EMPTY
    with pytest.raises(ParseError):
        await fetch(QUOTES, [http], parser=find_parser(QUOTES))


async def test_a_busy_browser_falls_back_to_the_plain_page_and_forgets_the_render(http, browser):
    await save_profile("quotes.toscrape.com", SiteProfile(render=["parse_quotes"]))
    browser.result = BrowserBusyError("busy")
    http.result = Page(QUOTES, 200, QUOTE_HTML)  # the memory was wrong: the plain page has the data now
    assert await fetch(QUOTES, [http, browser], parser=find_parser(QUOTES)) == [
        {"quote": "Hi", "author": "Ann", "tags": []}
    ]
    assert (await get_profile("quotes.toscrape.com")).render == []


async def test_when_neither_has_the_data_the_browser_error_stands(http, browser):
    await save_profile("quotes.toscrape.com", SiteProfile(render=["parse_quotes"]))
    browser.result, http.result = BrowserBusyError("busy"), EMPTY
    with pytest.raises(BrowserBusyError):
        await fetch(QUOTES, [http, browser], parser=find_parser(QUOTES))


async def test_an_error_page_is_not_sent_to_the_browser(http, browser):
    http.result = Page(QUOTES, 404, b"<html>gone</html>")
    with pytest.raises(ParseError):
        await fetch(QUOTES, [http, browser], parser=find_parser(QUOTES))
    assert browser.requests == []
