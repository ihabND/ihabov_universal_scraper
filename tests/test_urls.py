import pytest

from scraper.errors import BlockedURLError, InvalidURLError
from scraper.urls import normalize_url, validate_url


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
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
    ],
)
def test_internal_addresses_are_blocked(url):
    with pytest.raises(BlockedURLError):
        validate_url(url)


def test_only_http_and_https_are_allowed():
    with pytest.raises(InvalidURLError):
        validate_url("file:///etc/passwd")
