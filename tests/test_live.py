"""Every parser's example page, scraped from the real site (pytest -m live). Shows when a site has changed."""

import pytest

from scraper.core.service import Scraper
from scraper.fetchers import build_tiers
from scraper.net.guard import EgressGuard
from scraper.parsers import PARSERS

pytestmark = [pytest.mark.anyio, pytest.mark.live]


@pytest.mark.parametrize("parser", PARSERS, ids=lambda p: p.name)
async def test_the_example_page_parses(parser, ignore_socket_resets):
    guard = EgressGuard()
    scraper = Scraper(build_tiers(guard), guard)
    await scraper.start()
    try:
        result = await scraper.scrape(parser.example, "json")
    finally:
        await scraper.stop()
    assert result.ok and result.parsed
