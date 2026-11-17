"""What the scraper remembers about each site: whether plain requests get blocked, and which
parsers need the browser to see their data."""

import time

from scraper import cache, profiles
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
