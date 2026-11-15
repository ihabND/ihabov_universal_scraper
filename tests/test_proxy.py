from scraper import proxy
from scraper.proxy import playwright_proxy, with_scheme


def test_missing_scheme_defaults_to_http():
    url = with_scheme("login__cr.rs:secret@gw.dataimpulse.com:823")
    assert url == "http://login__cr.rs:secret@gw.dataimpulse.com:823"
    assert playwright_proxy(url) == {
        "server": "http://gw.dataimpulse.com:823",
        "username": "login__cr.rs",
        "password": "secret",
    }
    assert with_scheme("socks5://proxy.example.com:1080") == "socks5://proxy.example.com:1080"
    assert with_scheme("") is None


def test_no_proxy_configured():
    assert playwright_proxy(None) is None
    assert playwright_proxy("") is None


def test_credentials_are_split_out_of_the_server():
    assert playwright_proxy("http://user:secret@proxy.example.com:8080") == {
        "server": "http://proxy.example.com:8080",
        "username": "user",
        "password": "secret",
    }


def test_proxy_without_credentials():
    assert playwright_proxy("http://proxy.example.com:8080") == {"server": "http://proxy.example.com:8080"}


def test_percent_encoded_credentials_are_decoded():
    # Providers put session and country options in the username, and passwords can contain
    # characters such as @ or : that must be percent-encoded inside a URL.
    assert playwright_proxy("http://user-country-gb:p%40ss%3Aword@gate.example.com:7000") == {
        "server": "http://gate.example.com:7000",
        "username": "user-country-gb",
        "password": "p@ss:word",
    }


def test_socks_scheme_is_kept():
    assert playwright_proxy("socks5://proxy.example.com:1080") == {"server": "socks5://proxy.example.com:1080"}


# ---- Which requests use the proxy ----------------------------------------------------------------

PROXY = "http://user:pw@gw.example.net:823"


def test_practice_sites_skip_the_proxy(monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_URL", PROXY)
    for url in (
        "https://books.toscrape.com/catalogue/page-2.html",
        "https://quotes.toscrape.com/page/2/",
        "https://webscraper.io/test-sites/product/bmw",
        "https://scrapeme.live/shop/Bulbasaur/",
        "https://example.com/",
        "https://www.example.com/",
    ):
        assert proxy.proxy_for(url) is None, url


def test_real_sites_use_the_proxy(monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_URL", PROXY)
    assert proxy.proxy_for("https://www.imdb.com/title/tt0111161/") == PROXY
    # a host that only ends with the same letters is a different site
    assert proxy.proxy_for("https://evil-toscrape.com/") == PROXY
    assert proxy.proxy_for("https://toscrape.com.attacker.net/") == PROXY


def test_no_proxy_configured_means_direct_for_everything(monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_URL", None)
    assert proxy.proxy_for("https://www.imdb.com/") is None
    assert proxy.context_proxy("https://www.imdb.com/") is None


def test_browser_context_gets_the_proxy_in_playwright_form(monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_URL", PROXY)
    assert proxy.context_proxy("https://www.imdb.com/") == {
        "server": "http://gw.example.net:823",
        "username": "user",
        "password": "pw",
    }
    assert proxy.context_proxy("https://books.toscrape.com/") is None


def test_bypass_list_can_be_set_from_the_environment():
    assert proxy.parse_bypass(" Example.org, .shop.test ,,") == ("example.org", "shop.test")
    assert proxy.parse_bypass("") == proxy.DEFAULT_BYPASS
