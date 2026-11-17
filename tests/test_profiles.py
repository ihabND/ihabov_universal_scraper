"""What the scraper remembers about each site: whether plain requests get blocked, and which
parsers need the browser to see their data."""

import time
from types import SimpleNamespace

import pytest

from scraper import cache, profiles
from scraper.errors import FetchError, UnsupportedContentError
from scraper.page import Page
from scraper.parsers import find_parser
from scraper.profiles import SiteProfile, get_profile


def test_an_unknown_site_has_an_empty_profile():
    assert get_profile("shop.example") == SiteProfile()


def test_imdb_starts_seeded_as_blocking():
    profile = get_profile("imdb.com")
    assert profile.blocks_plain and profile.seeded


def test_learning_overrides_the_seed():
    profiles.learn_not_blocked("imdb.com")
    profile = get_profile("imdb.com")
    assert not profile.blocks_plain and not profile.seeded


def test_learning_render_keeps_the_seeded_block():
    profiles.learn_needs_render("imdb.com", "parse_movie")
    profile = get_profile("imdb.com")
    assert profile.blocks_plain and profile.render == {"parse_movie"} and not profile.seeded


def test_a_parser_that_renders_plainly_is_forgotten():
    profiles.learn_needs_render("shop.example", "parse_book")
    profiles.learn_renders_plain("shop.example", "parse_book")
    assert get_profile("shop.example").render == set()


def test_without_redis_a_learned_profile_outlives_the_memory_minute(monkeypatch):
    profiles.learn_blocked("shop.example")
    later = time.monotonic() + profiles.PROFILE_MEMORY_SECONDS + 1
    monkeypatch.setattr(profiles.time, "monotonic", lambda: later)
    assert get_profile("shop.example").blocks_plain


def test_profiles_are_shared_through_redis(redis):
    profiles.learn_blocked("shop.example")
    profiles.clear_memory()  # another machine, or after a restart
    assert get_profile("shop.example").blocks_plain
    assert 0 < cache.redis_client.ttl("scrape:v2:site:shop.example") <= profiles.PROFILE_SECONDS


def test_memory_is_trusted_for_a_minute_then_redis_is_read_again(redis, monkeypatch):
    profiles.learn_blocked("shop.example")
    cache.set_json("site:shop.example", {"b": False, "r": [], "c": time.time()}, 600)  # another machine learned
    assert get_profile("shop.example").blocks_plain
    later = time.monotonic() + profiles.PROFILE_MEMORY_SECONDS + 1
    monkeypatch.setattr(profiles.time, "monotonic", lambda: later)
    assert not get_profile("shop.example").blocks_plain


def test_profiles_work_in_memory_while_redis_is_paused(redis, monkeypatch):
    monkeypatch.setattr(cache, "_paused_until", time.monotonic() + 30)
    profiles.learn_blocked("shop.example")
    assert get_profile("shop.example").blocks_plain


def test_a_broken_redis_record_reads_as_unknown(redis):
    cache.set_json("site:shop.example", {"b": "yes", "r": 5}, 600)
    assert get_profile("shop.example") == SiteProfile()


def test_returned_profiles_are_copies():
    get_profile("shop.example").render.add("parse_book")
    assert get_profile("shop.example").render == set()


# ---- Daily re-check --------------------------------------------------------------------------

QUOTES = "https://quotes.toscrape.com/page/2/"
QUOTE_PAGE = b'<div class="quote"><span class="text">Hi</span><small class="author">Ann</small></div>'


@pytest.fixture
def sync_pool(monkeypatch):
    """Runs re-checks right away instead of in the background."""
    monkeypatch.setattr(profiles, "recheck_pool", SimpleNamespace(submit=lambda fn, *a: fn(*a)))


def stale(site, **fields):
    profiles.save_profile(site, SiteProfile(**fields, checked_at=time.time() - 25 * 3600))


def test_a_stale_block_is_rechecked_and_cleared(sync_pool, monkeypatch):
    stale("shop.example", blocks_plain=True)
    monkeypatch.setattr(profiles, "fetch_page", lambda url: Page(url, 200, b"<html>open</html>"))
    profiles.maybe_recheck("shop.example", "https://shop.example/a")
    assert not profiles.get_profile("shop.example").blocks_plain


def test_a_stale_render_is_rechecked_and_cleared(sync_pool, monkeypatch):
    stale("quotes.toscrape.com", render={"parse_quotes"})
    monkeypatch.setattr(profiles, "fetch_page", lambda url: Page(url, 200, QUOTE_PAGE))
    profiles.maybe_recheck("quotes.toscrape.com", QUOTES, find_parser(QUOTES))
    assert profiles.get_profile("quotes.toscrape.com").render == set()


def test_a_fresh_profile_is_not_rechecked(sync_pool, monkeypatch):
    profiles.learn_blocked("shop.example")
    monkeypatch.setattr(profiles, "fetch_page", lambda url: pytest.fail("should not fetch"))
    profiles.maybe_recheck("shop.example", "https://shop.example/a")


def test_a_seed_is_never_rechecked(sync_pool, monkeypatch):
    monkeypatch.setattr(profiles, "fetch_page", lambda url: pytest.fail("should not fetch"))
    profiles.maybe_recheck("imdb.com", "https://www.imdb.com/title/tt0111161/")


def test_one_recheck_at_a_time_per_site(monkeypatch):
    submitted = []
    monkeypatch.setattr(profiles, "recheck_pool", SimpleNamespace(submit=lambda fn, *a: submitted.append(a)))
    stale("shop.example", blocks_plain=True)
    profiles.maybe_recheck("shop.example", "https://shop.example/a")
    profiles.maybe_recheck("shop.example", "https://shop.example/b")
    assert len(submitted) == 1


@pytest.mark.parametrize(
    "outcome", [FetchError("down"), UnsupportedContentError("pdf"), RuntimeError("bug"), "blocked"]
)
def test_a_failed_recheck_keeps_the_route(sync_pool, monkeypatch, outcome):
    stale("shop.example", blocks_plain=True)

    def fetch_page(url):
        if outcome == "blocked":
            return Page(url, 403, b"<title>Just a moment...</title>")
        raise outcome

    monkeypatch.setattr(profiles, "fetch_page", fetch_page)
    profiles.maybe_recheck("shop.example", "https://shop.example/a")
    assert profiles.get_profile("shop.example").blocks_plain
    assert "shop.example" not in profiles.rechecks_in_flight
