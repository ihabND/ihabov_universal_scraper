import asyncio

import pytest

from scraper.core.errors import BlockedURLError, FetchError, InvalidURLError
from scraper.net import guard as guard_module
from scraper.net import proxy, urls
from scraper.net.guard import EgressGuard
from scraper.net.urls import normalize_url, validate_url
from scraper.settings import load, settings

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.com", "https://example.com/"),
        ("  https://example.com/a b?q=1#part  ", "https://example.com/a%20b?q=1"),
        ("https://bücher.de/x", "https://xn--bcher-kva.de/x"),
        ("ftp://example.com/file", None),
        ("not a url", None),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8000/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
async def test_internal_addresses_are_blocked(url):
    with pytest.raises(BlockedURLError):
        await validate_url(url)


async def test_only_http_and_https_are_allowed():
    with pytest.raises(InvalidURLError):
        await validate_url("file:///etc/passwd")


async def test_a_host_that_does_not_resolve_is_a_fetch_error_not_forbidden():
    with pytest.raises(FetchError, match="resolve"):
        await validate_url("https://nothing.invalid/")


# ---- Egress guard ----------------------------------------------------------------------------


async def socks_connect(guard: EgressGuard, host: str, port: int):
    """Open a SOCKS5 CONNECT through the guard; returns (reply code, reader, writer)."""
    guard_host, guard_port = guard.address.split(":")
    reader, writer = await asyncio.open_connection(guard_host, int(guard_port))
    writer.write(b"\x05\x01\x00")
    assert await reader.readexactly(2) == b"\x05\x00"
    name = host.encode()
    writer.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    reply = await reader.readexactly(10)
    return reply[1], reader, writer


@pytest.fixture
async def echo_server():
    connections = []

    async def echo(reader, writer):
        connections.append(1)
        writer.write(await reader.readline())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1], connections
    server.close()


@pytest.fixture
async def guard(monkeypatch):
    monkeypatch.setattr(urls, "is_public", lambda ip: ip == "127.0.0.1")
    guard = EgressGuard()
    await guard.start()
    yield guard
    await guard.stop()


async def test_the_guard_relays_to_a_public_address(guard, echo_server):
    port, connections = echo_server
    code, reader, writer = await socks_connect(guard, "127.0.0.1", port)
    writer.write(b"hello\n")
    assert code == 0 and await reader.readline() == b"hello\n"
    assert connections == [1]
    writer.close()


async def test_the_guard_refuses_an_internal_address_without_connecting(guard, echo_server):
    port, connections = echo_server
    code, _, writer = await socks_connect(guard, "10.255.255.1", port)
    assert code == 2 and connections == []
    writer.close()


async def test_the_guard_tries_the_next_address_when_one_does_not_answer(guard, echo_server, monkeypatch):
    port, connections = echo_server

    async def two_addresses(host):
        return ["127.0.0.2", "127.0.0.1"]  # nothing listens on the first

    monkeypatch.setattr(urls, "resolve_public", two_addresses)
    monkeypatch.setattr(guard_module, "CONNECT_TIMEOUT_SECONDS", 0.5)
    code, reader, writer = await socks_connect(guard, "shop.example", port)
    writer.write(b"hello\n")
    assert code == 0 and await reader.readline() == b"hello\n"
    writer.close()


# ---- Proxy choice ----------------------------------------------------------------------------

PROXY = "http://user:pw@gw.example.net:823"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PROXY, {"server": "http://gw.example.net:823", "username": "user", "password": "pw"}),
        (
            "http://u-country-gb:p%40ss%3Aword@gate.example.com:7000",
            {"server": "http://gate.example.com:7000", "username": "u-country-gb", "password": "p@ss:word"},
        ),
        ("http://proxy.example.com:8080", {"server": "http://proxy.example.com:8080"}),
        ("socks5://proxy.example.com:1080", {"server": "socks5://proxy.example.com:1080"}),
    ],
)
def test_playwright_proxy_splits_out_credentials(url, expected):
    assert proxy.playwright_proxy(url) == expected


def test_a_proxy_without_a_scheme_is_http(monkeypatch):
    monkeypatch.setenv("PROXY_URL", "login:secret@gw.dataimpulse.com:823")
    assert load().proxy_url == "http://login:secret@gw.dataimpulse.com:823"


def test_practice_sites_and_their_subdomains_skip_the_proxy(monkeypatch):
    monkeypatch.setattr(settings, "proxy_url", PROXY)
    assert proxy.upstream_for("https://books.toscrape.com/") is None
    assert proxy.upstream_for("https://www.example.com/") is None
    assert proxy.upstream_for("https://evil-toscrape.com/") == PROXY
    assert proxy.upstream_for("https://toscrape.com.attacker.net/") == PROXY


def test_without_an_upstream_proxy_everything_goes_through_the_guard():
    guard = EgressGuard()
    guard.address = "127.0.0.1:9999"
    assert proxy.curl_proxy("https://www.imdb.com/", guard) == "socks5h://127.0.0.1:9999"
    assert proxy.browser_proxy("https://www.imdb.com/", guard) == {"server": "socks5://127.0.0.1:9999"}


def test_an_upstream_proxy_replaces_the_guard(monkeypatch):
    monkeypatch.setattr(settings, "proxy_url", PROXY)
    guard = EgressGuard()
    assert proxy.curl_proxy("https://www.imdb.com/", guard) == PROXY
    assert proxy.browser_proxy("https://www.imdb.com/", guard)["server"] == "http://gw.example.net:823"
