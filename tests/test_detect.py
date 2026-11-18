import pytest

from scraper.antibot.detect import challenge_of, looks_blocked
from scraper.core.page import Page

AWS_WAF = b"<html><script src='https://x.awswaf.com/challenge.js'></script></html>"


@pytest.mark.parametrize(
    ("content", "vendor"),
    [
        (AWS_WAF, "aws-waf"),
        (b"<script>window._cf_chl_opt = {}</script>", "cloudflare"),
        (b"<title>Just a moment...</title>", "cloudflare"),
        (b"<script src='https://geo.captcha-delivery.com/c.js'></script>", "datadome"),
        (b"<html><body>hello</body></html>", None),
    ],
)
def test_challenge_vendor(content, vendor):
    assert challenge_of(content) == vendor


def test_a_large_page_is_never_a_challenge():
    assert challenge_of(AWS_WAF + b"x" * 200_000) is None


def test_blocking_statuses():
    assert looks_blocked(403, b"<html>no</html>") and looks_blocked(429, b"") and looks_blocked(202, b"")
    assert not looks_blocked(404, b"<html>gone</html>")


def test_a_bot_check_page_is_not_ok():
    page = Page("https://www.imdb.com/title/tt1/", 202, AWS_WAF)
    assert page.blocked and not page.ok


def test_ok_means_a_2xx_page_that_is_not_blocked():
    assert Page("https://a.example/", 200, b"<html>x</html>").ok
    assert not Page("https://a.example/", 404, b"<html>gone</html>").ok
