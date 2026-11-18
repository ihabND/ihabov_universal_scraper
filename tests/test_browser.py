"""The browser fetcher. Most tests use small fakes; the ones marked `browser` need Chromium (pytest -m browser)."""

import asyncio
import http.server
import threading

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scraper.core.errors import BrowserBusyError, FetchError
from scraper.fetchers.base import FetchRequest
from scraper.fetchers.browser import BrowserFetcher, keeps_cookies
from scraper.net import urls
from scraper.net.guard import EgressGuard

pytestmark = pytest.mark.anyio


class SlowTab:
    """A page that never reaches network idle, and shows `pages` one after another."""

    url = "https://shop.example/"

    def __init__(self, *pages: bytes, timeout_error=PlaywrightTimeoutError):
        self.pages, self.timeout_error = list(pages), timeout_error

    async def content(self) -> str:
        return (self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]).decode()

    async def wait_for_load_state(self, state, timeout):
        await asyncio.sleep(timeout / 1000)
        raise self.timeout_error("still loading")


async def test_json_mode_stops_as_soon_as_the_data_is_there(monkeypatch):
    monkeypatch.setattr("scraper.fetchers.browser.RENDER_POLL_MS", 10)
    tab = SlowTab(b"<html>loading</html>", b"<html>data</html>")
    fetcher = BrowserFetcher(EgressGuard(), warm=False)
    content, complete = await fetcher.wait_for_render(tab, b"<html>loading</html>", lambda c: b"data" in c, 5)
    assert content == b"<html>data</html>" and complete is False


async def test_html_mode_waits_for_the_page_to_settle():
    fetcher = BrowserFetcher(EgressGuard(), warm=False)
    content, complete = await fetcher.wait_for_render(
        SlowTab(b"<html>full</html>"), b"<html>partial</html>", None, 0.05
    )
    assert content == b"<html>full</html>" and complete is True


class OtherEngine:
    """Stands in for patchright, whose error classes are not Playwright's."""

    Error = type("Error", (Exception,), {})
    TimeoutError = type("TimeoutError", (Error,), {})


async def test_the_selected_engine_s_errors_are_the_ones_handled():
    fetcher = BrowserFetcher(EgressGuard(), warm=False)
    fetcher.api = OtherEngine
    tab = SlowTab(b"<html>full</html>", timeout_error=OtherEngine.TimeoutError)
    content, complete = await fetcher.wait_for_render(tab, b"<html>partial</html>", None, 0.05)
    assert content == b"<html>full</html>" and complete is True


class StuckTab:
    """A bot-check page that never changes."""

    url = "https://shop.example/"

    def __init__(self, content: bytes):
        self.page, self.waits = content, 0

    async def content(self) -> str:
        return self.page.decode()

    async def wait_for_timeout(self, ms):
        self.waits += 1
        await asyncio.sleep(0.05)


DATADOME = b"<script src='https://geo.captcha-delivery.com/i/x.js'></script>"


async def test_a_captcha_no_solver_can_pass_is_not_waited_out():
    tab = StuckTab(DATADOME)
    content, challenged = await BrowserFetcher(EgressGuard(), warm=False).wait_out_challenge(tab, 20)
    assert challenged and tab.waits == 0


async def test_a_check_that_can_clear_by_itself_is_waited_for():
    tab = StuckTab(b"<title>Just a moment...</title>")
    await BrowserFetcher(EgressGuard(), warm=False).wait_out_challenge(tab, 0.2)
    assert tab.waits >= 2


async def test_a_captcha_waits_while_a_registered_solver_works_on_it(monkeypatch):
    class DataDomeSolver:
        vendors = {"datadome"}

        async def solve(self, tab, vendor):
            pass

    monkeypatch.setattr("scraper.antibot.solvers.SOLVERS", [DataDomeSolver()])
    tab = StuckTab(DATADOME)
    await BrowserFetcher(EgressGuard(), warm=False).wait_out_challenge(tab, 0.2)
    assert tab.waits >= 2


def test_cookies_are_kept_only_after_a_block_or_a_bot_check():
    page, challenge = b"<html>ok</html>", b"<title>Just a moment...</title>"
    assert keeps_cookies(after_block=True, challenged=False, content=page)
    assert keeps_cookies(after_block=False, challenged=True, content=page)
    assert not keeps_cookies(after_block=False, challenged=False, content=page)
    assert not keeps_cookies(after_block=True, challenged=True, content=challenge)


async def test_a_busy_browser_answers_503_after_the_queue_timeout(monkeypatch):
    monkeypatch.setattr(urls, "is_public", lambda ip: True)
    fetcher = BrowserFetcher(EgressGuard(), workers=1, warm=False)
    await fetcher.slots.acquire()
    with pytest.raises(BrowserBusyError):
        await fetcher.fetch(FetchRequest("http://127.0.0.1/", queue_timeout=0.01))


# ---- With a real Chromium ------------------------------------------------------------------


@pytest.fixture
def two_sites():
    """127.0.0.1 counts as public and redirects to 127.0.0.2, which doesn't; each records its visits."""
    visits = {"public": [], "internal": []}

    def handler(name, internal_port=None):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                visits[name].append(self.path)
                if self.path == "/to-internal":
                    self.send_response(302)
                    self.send_header("location", f"http://127.0.0.2:{internal_port}/admin")
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                body = b"<html><body><h1>hello</h1></body></html>"
                self.send_response(200)
                self.send_header("content-type", "text/html")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    internal = http.server.ThreadingHTTPServer(("127.0.0.2", 0), handler("internal"))
    public = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler("public", internal.server_address[1]))
    for server in (internal, public):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{public.server_address[1]}", visits
    for server in (internal, public):
        server.shutdown()


@pytest.fixture
async def browser(monkeypatch, ignore_socket_resets):
    monkeypatch.setattr(urls, "is_public", lambda ip: ip == "127.0.0.1")
    guard = EgressGuard()
    await guard.start()
    fetcher = BrowserFetcher(guard, workers=1, warm=False)
    yield fetcher
    await fetcher.stop()
    await guard.stop()


@pytest.mark.browser
async def test_the_browser_loads_a_page_through_the_guard(browser, two_sites):
    site, _ = two_sites
    page = await browser.fetch(FetchRequest(site + "/hello"))
    assert (page.status_code, page.fetched_with) == (200, "browser") and b"<h1>hello</h1>" in page.content


@pytest.mark.browser
async def test_a_redirect_to_an_internal_address_never_reaches_it(browser, two_sites):
    site, visits = two_sites
    with pytest.raises(FetchError):
        await browser.fetch(FetchRequest(site + "/to-internal"))
    assert visits["public"] == ["/to-internal"] and visits["internal"] == []


class MissingEngine:
    def async_playwright(self):
        raise ImportError("no browser engine installed")


async def test_a_browser_that_cannot_start_does_not_stop_the_server_and_shows_in_health():
    fetcher = BrowserFetcher(EgressGuard(), warm=True)
    fetcher.api = MissingEngine()
    await fetcher.start()  # returns at once; the browser starts in the background
    with pytest.raises(FetchError):
        await fetcher.warming
    assert fetcher.status.startswith("failed: ")


@pytest.mark.browser
async def test_another_browser_tier_reports_its_own_name(browser, two_sites):
    site, _ = two_sites
    stealth = BrowserFetcher(browser.guard, name="stealth", workers=1, warm=False)
    try:
        assert (await stealth.fetch(FetchRequest(site + "/hello"))).fetched_with == "stealth"
    finally:
        await stealth.stop()
