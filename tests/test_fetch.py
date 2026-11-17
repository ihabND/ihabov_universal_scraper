"""When the scraper uses plain HTTP, when it uses the browser, and what it remembers in between.
The real fetchers are replaced by fakes, so these tests only exercise the decisions."""

import time

import pytest

from scraper import blocking, profiles
from scraper import fetch as fetch_module
from scraper.errors import FetchError
from scraper.fetch import fetch
from scraper.page import Page

URL = "https://shop.example/item"
OPEN = Page(URL, 200, b"<html>open</html>")
BLOCKED = Page(URL, 403, b"<title>Just a moment...</title>")
RENDERED = Page(URL, 200, b"<html>rendered</html>", "browser")
IMDB = "https://www.imdb.com/title/tt0111161/"


@pytest.fixture
def fake(monkeypatch):
    """Records each plain fetch (with the cookies it was given) and each browser run."""
    state = {"http": [], "browser": 0, "http_result": OPEN, "browser_result": RENDERED}

    def plain(url, clearance=None):
        state["http"].append(clearance)
        return state["http_result"]

    def browser(url, **options):
        state["browser"] += 1
        if isinstance(state["browser_result"], Exception):
            raise state["browser_result"]
        return state["browser_result"]

    monkeypatch.setattr(fetch_module, "fetch_page", plain)
    monkeypatch.setattr(fetch_module, "fetch_with_browser", browser)
    return state


def test_an_open_site_never_starts_the_browser(fake):
    assert fetch(URL) is OPEN
    assert fake["browser"] == 0


def test_a_blocked_site_goes_to_the_browser_and_is_remembered(fake):
    fake["http_result"] = BLOCKED
    assert fetch(URL) is RENDERED
    assert fetch(URL) is RENDERED
    assert len(fake["http"]) == 1  # the second time, no plain attempt was wasted
    assert fake["browser"] == 2


def test_browser_never_returns_the_blocked_page(fake):
    fake["http_result"] = BLOCKED
    assert fetch(URL, "never") is BLOCKED
    assert fake["browser"] == 0


def test_a_remembered_site_still_gets_a_plain_fetch_when_the_browser_fails(fake):
    profiles.learn_blocked("shop.example")
    fake["browser_result"] = FetchError("browser crashed")
    assert fetch(URL) is OPEN  # the site stopped blocking, so the plain page is fine
    assert not profiles.get_profile("shop.example").blocks_plain

    profiles.learn_blocked("shop.example")
    fake["http_result"] = BLOCKED
    with pytest.raises(FetchError, match="browser crashed"):  # still blocked: the browser's error stands
        fetch(URL)


def test_saved_bot_check_cookies_are_only_used_by_their_owner(fake):
    blocking.save_clearance("alice", "shop.example", [{"name": "cf_clearance", "value": "x"}], {})
    fetch(URL, owner="alice")
    fetch(URL, owner="bob")
    alice, bob = fake["http"]
    assert alice is not None and alice.cookies[0]["value"] == "x"
    assert bob is None


def test_a_seeded_site_goes_to_the_browser_first(fake):
    assert fetch(IMDB) is RENDERED
    assert fake["http"] == [] and fake["browser"] == 1


def test_cookies_beat_the_profile(fake):
    profiles.learn_blocked("shop.example")
    blocking.save_clearance("alice", "shop.example", [{"name": "aws-waf-token", "value": "x"}], {})
    assert fetch(URL, owner="alice") is OPEN
    assert fake["browser"] == 0 and fake["http"][0] is not None


def test_browser_never_ignores_the_profile(fake):
    profiles.learn_blocked("shop.example")
    fake["http_result"] = BLOCKED
    assert fetch(URL, "never") is BLOCKED
    assert fetch(IMDB, "never") is BLOCKED
    assert fake["browser"] == 0


def test_the_learned_block_outlives_ten_minutes(fake, monkeypatch):
    fake["http_result"] = BLOCKED
    fetch(URL)
    later = time.monotonic() + 11 * 60  # the old needs-browser flag would have expired
    monkeypatch.setattr(profiles.time, "monotonic", lambda: later)
    fetch(URL)
    assert len(fake["http"]) == 1


def test_a_site_that_fails_in_the_browser_too_is_not_learned_as_blocking(fake):
    fake["http_result"] = Page(URL, 503, b"<html>down for maintenance</html>")
    fake["browser_result"] = Page(URL, 503, b"<html>down for maintenance</html>", "browser")
    fetch(URL)
    assert not profiles.get_profile("shop.example").blocks_plain
