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
