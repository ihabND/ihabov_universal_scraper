from scraper.proxy import playwright_proxy


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
