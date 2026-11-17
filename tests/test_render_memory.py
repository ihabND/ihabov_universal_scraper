"""JSON mode remembers parsers whose data only appears after the browser runs the page, and sends
their next requests straight to the browser."""

from types import SimpleNamespace

import pytest

from scraper import profiles, service
from scraper.errors import ParseError
from scraper.page import Page
from scraper.service import scrape

QUOTES = "https://quotes.toscrape.com/page/2/"
EMPTY = Page(QUOTES, 200, b"<html></html>")  # the plain page, before JavaScript adds the quotes
RENDERED = Page(
    QUOTES, 200, b'<div class="quote"><span class="text">Hi</span><small class="author">Ann</small></div>', "browser"
)


@pytest.fixture
def fakes(monkeypatch):
    state = SimpleNamespace(plain_calls=0, browser_calls=0, rechecks=[])

    def fake_fetch(url, *args, **kwargs):
        state.plain_calls += 1
        return EMPTY

    def fake_browser(url, **kwargs):
        state.browser_calls += 1
        return RENDERED

    def reset():
        state.plain_calls = state.browser_calls = 0

    state.reset = reset
    monkeypatch.setattr(service, "fetch", fake_fetch)
    monkeypatch.setattr(service, "fetch_with_browser", fake_browser)
    monkeypatch.setattr(profiles, "recheck_pool", SimpleNamespace(submit=lambda fn, *a: state.rechecks.append(a)))
    return state


def test_a_parse_that_needed_the_browser_is_remembered(fakes):
    scrape(QUOTES, "json")
    assert profiles.get_profile("quotes.toscrape.com").render == {"parse_quotes"}
    fakes.reset()
    scrape(QUOTES, "json")
    assert fakes.plain_calls == 0 and fakes.browser_calls == 1


def test_html_mode_is_not_affected_by_render_memory(fakes):
    profiles.learn_needs_render("quotes.toscrape.com", "parse_quotes")
    scrape(QUOTES, "html")
    assert fakes.plain_calls == 1 and fakes.browser_calls == 0


def test_render_memory_respects_browser_never(fakes):
    profiles.learn_needs_render("quotes.toscrape.com", "parse_quotes")
    with pytest.raises(ParseError):
        scrape(QUOTES, "json", "never")
    assert fakes.browser_calls == 0
