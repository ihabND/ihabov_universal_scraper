"""The plain HTTP fetcher against a small local server, through the real egress guard."""

import http.server
import itertools
import threading

import pytest

from scraper.antibot.clearance import Clearance
from scraper.core.errors import BlockedURLError, ContentTooLargeError, FetchError, UnsupportedContentError
from scraper.core.strategy import Strategy
from scraper.fetchers import http as http_module
from scraper.fetchers.base import FetchRequest
from scraper.fetchers.http import HttpFetcher
from scraper.net import urls
from scraper.net.guard import EgressGuard

pytestmark = pytest.mark.anyio

hits = {"challenge": 0}
flaky = itertools.count()
CHALLENGE = b"<html><title>Just a moment...</title><script>_cf_chl_opt={}</script></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, body=b"", status=200, content_type="text/html", headers=()):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        routes = {
            "/ok": lambda: self.reply(b"<html><body>ok</body></html>"),
            "/redirect": lambda: self.reply(status=302, headers=[("location", "/ok")]),
            "/loop": lambda: self.reply(status=302, headers=[("location", "/loop")]),
            "/to-internal": lambda: self.reply(status=302, headers=[("location", "http://10.255.255.1/ok")]),
            "/pdf": lambda: self.reply(b"%PDF-1.7", content_type="application/pdf"),
            "/huge": lambda: self.reply(b"<p>" + b"x" * 5000),
            "/set-cookie": lambda: self.reply(
                status=302, headers=[("location", "/echo"), ("set-cookie", "s=1; Path=/")]
            ),
            "/echo": lambda: self.reply(f"cookie={self.headers.get('cookie')}".encode()),
            "/waf-403-json": lambda: self.reply(b'{"message": "blocked"}', 403, "application/json"),
        }
        if self.path == "/flaky":  # 503 twice, then the page
            return self.reply(status=503) if next(flaky) < 2 else self.reply(b"<html>finally</html>")
        if self.path == "/sets-cookie-then-loops" and "c=1" not in (self.headers.get("cookie") or ""):
            return self.reply(status=302, headers=[("location", self.path), ("set-cookie", "c=1")])  # no Path
        if self.path == "/sets-cookie-then-loops":
            return self.reply(b"<html>welcome back</html>")
        if self.path == "/dated-cookie-then-loops" and "s=2" not in (self.headers.get("cookie") or ""):
            dated = "s=2; path=/; expires=Sun, 18 Nov 2046 13:16:42 -0000"  # the date format curl_cffi drops
            return self.reply(status=302, headers=[("location", self.path), ("set-cookie", dated)])
        if self.path == "/dated-cookie-then-loops":
            return self.reply(b"<html>welcome back</html>")
        if self.path == "/echo-header":
            return self.reply(f"x-site={self.headers.get('x-site')}".encode())
        if self.path == "/to-localhost":  # the same server under another host name
            location = f"http://localhost:{self.server.server_address[1]}/echo-header"
            return self.reply(status=302, headers=[("location", location)])
        if self.path == "/user-agent":
            return self.reply(self.headers.get("user-agent", "").encode())
        if self.path == "/always-503":
            hits["always-503"] = hits.get("always-503", 0) + 1
            return self.reply(b"<html>down</html>", 503)
        if self.path == "/challenge-503":
            hits["challenge"] += 1
            return self.reply(CHALLENGE, 503)
        routes.get(self.path, lambda: self.reply(b"missing", 404))()


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
async def fetcher(monkeypatch):
    """The test server runs on 127.0.0.1, which is normally refused: allow exactly that address."""
    monkeypatch.setattr(urls, "is_public", lambda ip: ip == "127.0.0.1")
    monkeypatch.setattr(http_module, "BACKOFF_SECONDS", 0)
    guard = EgressGuard()
    await guard.start()
    fetcher = HttpFetcher(guard)
    yield fetcher
    await fetcher.stop()
    await guard.stop()


async def fetch(fetcher, url, **options):
    return await fetcher.fetch(FetchRequest(url, **options))


async def test_fetches_a_page(fetcher, site):
    page = await fetch(fetcher, site + "/ok")
    assert (page.status_code, page.content, page.fetched_with) == (200, b"<html><body>ok</body></html>", "http")


async def test_follows_redirects(fetcher, site):
    assert (await fetch(fetcher, site + "/redirect")).url == site + "/ok"


async def test_stops_redirect_loops(fetcher, site):
    with pytest.raises(FetchError, match="Too many redirects"):
        await fetch(fetcher, site + "/loop")


async def test_refuses_a_redirect_to_an_internal_address(fetcher, site):
    with pytest.raises(BlockedURLError):
        await fetch(fetcher, site + "/to-internal")


async def test_refuses_a_page_that_is_not_html(fetcher, site):
    with pytest.raises(UnsupportedContentError):
        await fetch(fetcher, site + "/pdf")


async def test_refuses_pages_over_the_size_limit(fetcher, site, monkeypatch):
    monkeypatch.setattr(http_module, "MAX_CONTENT_BYTES", 1000)
    with pytest.raises(ContentTooLargeError):
        await fetch(fetcher, site + "/huge")


async def test_retries_temporary_errors(fetcher, site):
    page = await fetch(fetcher, site + "/flaky")
    assert (page.status_code, page.content) == (200, b"<html>finally</html>")


async def test_a_bot_check_is_returned_without_retries(fetcher, site):
    page = await fetch(fetcher, site + "/challenge-503")
    assert page.blocked and hits["challenge"] == 1


async def test_a_block_with_a_non_html_body_is_returned_as_a_page(fetcher, site):
    page = await fetch(fetcher, site + "/waf-403-json")
    assert page.status_code == 403 and page.blocked


async def test_cookies_stay_within_one_fetch(fetcher, site):
    assert b"cookie=s=1" in (await fetch(fetcher, site + "/set-cookie")).content
    assert b"cookie=None" in (await fetch(fetcher, site + "/echo")).content


async def test_a_second_plain_tier_has_its_own_fingerprint_and_name(fetcher, site):
    safari = HttpFetcher(fetcher.guard, name="http-safari", impersonate="safari", retries=0)
    try:
        page = await fetch(safari, site + "/user-agent")
    finally:
        await safari.stop()
    assert page.fetched_with == "http-safari" and b"Safari" in page.content and b"Chrome" not in page.content


async def test_a_site_s_own_retries_and_headers_are_used_and_its_headers_stay_with_it(fetcher, site, monkeypatch):
    assert (await fetch(fetcher, site + "/always-503", strategy=Strategy(retries=0))).status_code == 503
    assert hits["always-503"] == 1
    page = await fetch(fetcher, site + "/echo-header", strategy=Strategy(headers={"x-site": "1"}))
    assert page.content == b"x-site=1"
    monkeypatch.setattr(urls, "is_public", lambda ip: ip in ("127.0.0.1", "::1"))
    page = await fetch(fetcher, site + "/to-localhost", strategy=Strategy(headers={"x-site": "1"}))
    assert page.content == b"x-site=None"


async def test_a_cookie_set_without_a_path_is_sent_on_the_next_hop(fetcher, site):
    # Like Goodreads: set a cookie, then redirect to the same page until the cookie comes back.
    assert (await fetch(fetcher, site + "/sets-cookie-then-loops")).content == b"<html>welcome back</html>"


async def test_a_cookie_with_any_expiry_format_is_sent_on_the_next_hop(fetcher, site):
    # Goodreads dates its session cookies "-0000", which curl_cffi can't parse and silently drops.
    assert (await fetch(fetcher, site + "/dated-cookie-then-loops")).content == b"<html>welcome back</html>"


async def test_saved_bot_check_cookies_are_sent_until_plain_http_failed_with_them(fetcher, site):
    cookie = {"name": "cf_clearance", "value": "x", "domain": "127.0.0.1", "path": "/"}
    assert b"cf_clearance=x" in (await fetch(fetcher, site + "/echo", clearance=Clearance([cookie], {}))).content
    failed = Clearance([cookie], {}, plain_blocked_at=9e12)
    assert b"cookie=None" in (await fetch(fetcher, site + "/echo", clearance=failed)).content


async def test_every_redirect_hop_chooses_its_own_proxy(fetcher, site, monkeypatch):
    asked = []
    real = http_module.curl_proxy
    monkeypatch.setattr(http_module, "curl_proxy", lambda url, *args: asked.append(url) or real(url, *args))
    await fetch(fetcher, site + "/redirect")
    assert asked == [site + "/redirect", site + "/ok"]
