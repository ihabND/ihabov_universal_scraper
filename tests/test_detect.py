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
        (b"<title>Reddit - Prove your humanity</title>", "reddit"),
        (b'<form method="post" action="/checkcaptcha?key=x">', "yandex"),
        (b"<title>Bot or Not?</title>", "expedia"),
        (b'var e=document.forms[0]; e.elements.namedItem("solution").value=n', "reddit-check"),
        (b'<script src="//g.alicdn.com/bsop-static/sufei-punish/0.1.130/build/punishpage.min.js">', "alibaba"),
        (b"<html><body>hello</body></html>", None),
    ],
)
def test_challenge_vendor(content, vendor):
    assert challenge_of(content) == vendor


def test_a_large_page_is_never_a_challenge():
    assert challenge_of(AWS_WAF + b"x" * 300_000) is None


def test_reddit_s_check_page_is_recognised_at_its_real_size():
    assert challenge_of(b"<title>Reddit - Prove your humanity</title>" + b"x" * 167_000) == "reddit"


def test_blocking_statuses():
    assert looks_blocked(403, b"<html>no</html>") and looks_blocked(429, b"") and looks_blocked(202, b"")
    assert looks_blocked(406, b"")  # CarGurus answers bots with an empty 406
    assert not looks_blocked(404, b"<html>gone</html>")


def test_a_bot_check_page_is_not_ok():
    page = Page("https://www.imdb.com/title/tt1/", 202, AWS_WAF)
    assert page.blocked and not page.ok


def test_ok_means_a_2xx_page_that_is_not_blocked():
    assert Page("https://a.example/", 200, b"<html>x</html>").ok
    assert not Page("https://a.example/", 404, b"<html>gone</html>").ok


def test_a_real_page_that_asks_for_javascript_in_noscript_is_not_a_bot_check():
    assert (
        challenge_of(
            b"<div>answers</div><noscript>Please enable Javascript and refresh the page to continue</noscript>"
        )
        is None
    )
