import time

import pytest

from scraper.antibot import clearance as clearance_module
from scraper.antibot import solvers
from scraper.antibot.clearance import Clearance, get_clearance, mark_plain_blocked, save_clearance
from scraper.store import cache

pytestmark = pytest.mark.anyio

COOKIE = {"name": "aws-waf-token", "value": "x", "domain": ".shop.example", "path": "/", "expires": -1}


async def test_cookies_are_kept_per_key_and_survive_a_restart(redis):
    await save_clearance("alice", "shop.example", Clearance([COOKIE], {"user-agent": "UA"}))
    cache.memory.clear()
    found = await get_clearance("alice", "shop.example")
    assert found.cookies == [COOKIE] and found.headers == {"user-agent": "UA"}
    assert await get_clearance("bob", "shop.example") is None
    assert await get_clearance(None, "shop.example") is None


async def test_cookies_past_their_own_expiry_are_dropped():
    old = {**COOKIE, "name": "old", "expires": time.time() - 1}
    await save_clearance("alice", "shop.example", Clearance([COOKIE, old], {}))
    assert [c["name"] for c in (await get_clearance("alice", "shop.example")).cookies] == ["aws-waf-token"]


async def test_plain_http_is_skipped_for_an_hour_after_it_was_blocked_with_the_cookies(monkeypatch):
    await save_clearance("alice", "shop.example", Clearance([COOKIE], {}))
    await mark_plain_blocked("alice", "shop.example", await get_clearance("alice", "shop.example"))
    await save_clearance("alice", "shop.example", Clearance([COOKIE], {}))  # renewed by the next browser run
    assert not (await get_clearance("alice", "shop.example")).plain_ok
    later = time.time() + clearance_module.PLAIN_RETRY_SECONDS + 1
    monkeypatch.setattr(clearance_module.time, "time", lambda: later)
    assert (await get_clearance("alice", "shop.example")).plain_ok


async def test_a_solver_runs_only_for_its_vendor(monkeypatch):
    calls = []

    class FakeSolver:
        vendors = {"cloudflare"}

        async def solve(self, tab, vendor):
            calls.append(vendor)

    monkeypatch.setattr(solvers, "SOLVERS", [FakeSolver()])
    await solvers.run_solvers(tab=None, vendor="aws-waf")
    await solvers.run_solvers(tab=None, vendor="cloudflare")
    assert calls == ["cloudflare"]


async def test_akamai_bot_manager_cookies_are_not_kept():
    # Replayed over plain HTTP they got flagged, and took the browser down with them (eBay, Expedia, Hotels.com).
    await save_clearance("alice", "shop.example", Clearance([COOKIE, {"name": "_abck", "value": "x"}], {}))
    assert await get_clearance("alice", "shop.example") is None
