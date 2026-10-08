"""The plain HTTP fetcher against a small local server."""

import http.server
import itertools
import threading

import pytest

from scraper import http_fetch, urls
from scraper.errors import BlockedURLError, ContentTooLargeError, FetchError, UnsupportedContentError
from scraper.http_fetch import fetch_page

flaky_attempts = itertools.count()


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
        port = self.server.server_address[1]
        routes = {
            "/ok": lambda: self.reply(b"<html><body>ok</body></html>"),
            "/redirect": lambda: self.reply(status=302, headers=[("location", "/ok")]),
            "/loop": lambda: self.reply(status=302, headers=[("location", "/loop")]),
            # a public page redirecting to an internal address must be refused
            "/to-internal": lambda: self.reply(status=302, headers=[("location", f"http://localhost:{port}/ok")]),
            "/pdf": lambda: self.reply(b"%PDF-1.7", content_type="application/pdf"),
            "/huge": lambda: self.reply(b"<p>" + b"x" * 5000),
            "/set-cookie": lambda: self.reply(
                status=302, headers=[("location", "/echo"), ("set-cookie", "s=1; Path=/")]
            ),
            "/echo": lambda: self.reply(f"cookie={self.headers.get('cookie')}".encode()),
        }
        if self.path == "/flaky":  # 503 twice, then the page
            return self.reply(status=503) if next(flaky_attempts) < 2 else self.reply(b"<html>finally</html>")
        routes.get(self.path, lambda: self.reply(b"missing", 404))()


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(autouse=True)
def local_server_is_public(monkeypatch):
    """The test server runs on 127.0.0.1, which the scraper normally refuses. Allow exactly that address;
    'localhost' stays blocked, which the redirect test relies on."""
    monkeypatch.setattr(urls, "is_public_host", lambda host, cache=None: host == "127.0.0.1")
    monkeypatch.setattr(http_fetch, "BACKOFF_SECONDS", 0)


def test_fetches_a_page(site):
    page = fetch_page(site + "/ok")
    assert (page.status_code, page.content) == (200, b"<html><body>ok</body></html>")


def test_follows_redirects(site):
    assert fetch_page(site + "/redirect").url == site + "/ok"


def test_stops_redirect_loops(site):
    with pytest.raises(FetchError, match="Too many redirects"):
        fetch_page(site + "/loop")


def test_refuses_a_redirect_to_an_internal_address(site):
    with pytest.raises(BlockedURLError):
        fetch_page(site + "/to-internal")


def test_refuses_non_html(site):
    with pytest.raises(UnsupportedContentError):
        fetch_page(site + "/pdf")


def test_refuses_pages_over_the_size_limit(site, monkeypatch):
    monkeypatch.setattr(http_fetch, "MAX_CONTENT_BYTES", 1000)
    with pytest.raises(ContentTooLargeError):
        fetch_page(site + "/huge")


def test_retries_temporary_errors(site):
    page = fetch_page(site + "/flaky")
    assert (page.status_code, page.content) == (200, b"<html>finally</html>")


def test_cookies_stay_within_one_fetch(site):
    assert b"cookie=s=1" in fetch_page(site + "/set-cookie").content  # kept along its own redirect chain
    assert b"cookie=None" in fetch_page(site + "/echo").content  # never sent on the next fetch
