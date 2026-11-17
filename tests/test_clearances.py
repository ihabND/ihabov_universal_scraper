"""Cookies from passed bot checks: kept per API key, in Redis when it is there, so they survive
restarts and are shared by every machine, but never used for another key."""

import time

from scraper import blocking, cache

COOKIE = {"name": "aws-waf-token", "value": "x", "domain": ".shop.example", "path": "/", "expires": -1}


def test_cookies_survive_a_restart_through_redis(redis):
    blocking.save_clearance("alice", "shop.example", [COOKIE], {"user-agent": "UA"})
    blocking.clearances.clear()
    found = blocking.get_clearance("alice", "shop.example")
    assert found.cookies == [COOKIE] and found.headers == {"user-agent": "UA"}
    assert blocking.get_clearance("bob", "shop.example") is None


def test_cookies_live_four_hours(redis):
    blocking.save_clearance("alice", "shop.example", [COOKIE], {})
    assert 4 * 3600 - 5 < cache.redis_client.ttl("scrape:v2:clearance:alice:shop.example") <= 4 * 3600


def test_cookies_past_their_own_expiry_are_dropped(redis):
    old = {**COOKIE, "name": "old", "expires": time.time() - 1}
    blocking.save_clearance("alice", "shop.example", [COOKIE, old], {})
    blocking.clearances.clear()
    assert [c["name"] for c in blocking.get_clearance("alice", "shop.example").cookies] == ["aws-waf-token"]


def test_a_failed_plain_attempt_is_remembered_across_restarts(redis):
    blocking.save_clearance("alice", "shop.example", [COOKIE], {})
    blocking.note_http_result("alice", "shop.example", blocking.get_clearance("alice", "shop.example"), False)
    blocking.clearances.clear()
    assert blocking.get_clearance("alice", "shop.example").http_works is False


def test_a_malformed_record_reads_as_none(redis):
    cache.set_json("clearance:alice:shop.example", {"cookies": "nope"}, 600)
    assert blocking.get_clearance("alice", "shop.example") is None


def test_cookies_work_from_memory_without_redis():
    blocking.save_clearance("alice", "shop.example", [COOKIE], {})
    assert blocking.get_clearance("alice", "shop.example") is not None


def test_cookies_get_a_fresh_plain_try_an_hour_after_failing(monkeypatch):
    blocking.save_clearance("alice", "shop.example", [COOKIE], {})
    blocking.note_http_result("alice", "shop.example", blocking.get_clearance("alice", "shop.example"), False)
    blocking.save_clearance("alice", "shop.example", [COOKIE], {})  # renewed by the next browser run
    assert blocking.get_clearance("alice", "shop.example").http_works is False
    later = time.time() + blocking.HTTP_RETRY_SECONDS + 1
    monkeypatch.setattr(blocking.time, "time", lambda: later)
    blocking.save_clearance("alice", "shop.example", [COOKIE], {})
    assert blocking.get_clearance("alice", "shop.example").http_works is None
