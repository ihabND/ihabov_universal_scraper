"""Scraping one URL or a batch: the page cache, the fetchers, parsing, and the response."""

import asyncio
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..api.schemas import BatchItem, BatchResponse, BrowserOption, Mode, ScrapeResponse
from ..fetchers.base import Fetcher
from ..net.guard import EgressGuard
from ..net.urls import normalize_url, site_of
from ..parsers import PARSERS, Parser, find_parser, parse_page
from ..settings import settings
from ..store.cache import get_page, set_page
from . import ladder
from .errors import BrowserDisabledError, InvalidURLError, ParseError, ScrapeError, UnsupportedSiteError
from .page import Page

QUEUE_SECONDS = 15  # how long a request waits for a free browser
BATCH_QUEUE_SECONDS = 120  # batch URLs wait longer
BATCH_PER_SITE = 4  # at most this many batch requests to one site at once
BATCH_SITE_GAP_SECONDS = 0.25  # started at least this far apart

logger = logging.getLogger(__name__)


class Scraper:
    def __init__(self, tiers: list[Fetcher], guard: EgressGuard):
        self.tiers, self.guard = tiers, guard
        self.inflight: dict[tuple, asyncio.Task] = {}
        self.site_gate = SiteGate(BATCH_PER_SITE, BATCH_SITE_GAP_SECONDS)
        self.batch_slots = defaultdict(lambda: asyncio.Semaphore(settings.batch_workers))  # per API key

    async def start(self) -> None:
        await self.guard.start()
        for tier in self.tiers:
            await tier.start()

    async def stop(self) -> None:
        for tier in self.tiers:
            await tier.stop()
        await self.guard.stop()

    @property
    def browser_status(self) -> str:
        return next((tier.status for tier in self.tiers if tier.renders), "off")

    def tiers_for(self, browser: BrowserOption) -> list[Fetcher]:
        if browser == "never":
            return [tier for tier in self.tiers if not tier.renders]
        if browser == "always":
            tiers = [tier for tier in self.tiers if tier.renders]
            if not tiers:
                raise BrowserDisabledError("Browser fetching is turned off on this server.")
            return tiers
        return self.tiers

    async def scrape(
        self,
        url: str,
        mode: Mode = "html",
        browser: BrowserOption = "auto",
        *,
        owner: str | None = None,
        gate=None,
        queue_timeout: float = QUEUE_SECONDS,
    ) -> ScrapeResponse:
        """Identical requests running at the same time share one scrape."""
        normalized = normalize_url(url)
        if normalized is None:
            raise InvalidURLError("Invalid URL, only HTTP and HTTPS.")
        parser = find_parser(normalized) if mode == "json" else None
        if mode == "json" and parser is None:
            supported = ", ".join(dict.fromkeys(p.host for p in PARSERS))
            raise UnsupportedSiteError(f"JSON isn't available for this page. Supported sites: {supported}.")
        tiers = self.tiers_for(browser)
        key = (normalized, mode, browser)
        task = self.inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self.run(normalized, browser, parser, tiers, owner, gate, queue_timeout))
            self.inflight[key] = task

            def finished(done: asyncio.Task) -> None:
                del self.inflight[key]
                if not done.cancelled():
                    done.exception()  # marks a failure as seen even when every caller gave up

            task.add_done_callback(finished)
        return await asyncio.shield(task)  # a caller that gives up doesn't cancel it for the others

    async def run(self, url, browser, parser: Parser | None, tiers, owner, gate, queue_timeout) -> ScrapeResponse:
        page, parsed = await get_page(url, browser), None
        if page is not None and parser is not None:
            try:
                parsed = await asyncio.to_thread(parse_page, parser, page)
            except ParseError:
                page = None
        elif page is not None and not page.complete:
            page = None  # a browser stopped early for JSON mode: content that loads later may be missing
        if page is None:
            page, parsed = await ladder.fetch(
                url, tiers, parser=parser, owner=owner, gate=gate, queue_timeout=queue_timeout
            )
            await set_page(url, browser, page)
        result = ScrapeResponse(url=url, status_code=page.status_code, ok=page.ok, fetched_with=page.fetched_with)
        if parser:
            result.parsed = parsed
        else:
            result.content = await asyncio.to_thread(prettify, page)
        return result

    async def batch(self, urls: list[str], mode: Mode, browser: BrowserOption, owner: str) -> BatchResponse:
        """Every URL is scraped as a single request would be; failures are reported per URL."""
        slots = self.batch_slots[owner]

        async def one(url: str) -> BatchItem:
            async with slots:
                try:
                    result = await self.scrape(
                        url, mode, browser, owner=owner, gate=self.site_gate, queue_timeout=BATCH_QUEUE_SECONDS
                    )
                    return BatchItem(url=url, status=200, result=result)
                except ScrapeError as e:
                    return BatchItem(url=url, status=e.status_code, detail=str(e))
                except Exception:  # one broken URL must not sink the batch
                    logger.exception("Batch item %s failed", url)
                    return BatchItem(url=url, status=500, detail="Internal error while scraping this URL.")

        unique: dict[str, str] = {}  # the same page sent twice is scraped once
        for url in urls:
            unique.setdefault(normalize_url(url) or url, url)
        done = dict(zip(unique, await asyncio.gather(*map(one, unique.values())), strict=True))
        results = [done[normalize_url(url) or url].model_copy(update={"url": url}) for url in urls]
        succeeded = sum(item.status == 200 for item in results)
        return BatchResponse(results=results, succeeded=succeeded, failed=len(results) - succeeded)


def prettify(page: Page) -> str:
    return BeautifulSoup(page.content, "lxml").prettify()


@dataclass
class GatedSite:
    slots: asyncio.Semaphore
    next_start: float = 0.0
    users: int = 0


@dataclass
class SiteGate:
    """Politeness for batches: at most `per_site` requests to one site at once, started `gap` seconds apart."""

    per_site: int
    gap: float
    sites: dict[str, GatedSite] = field(default_factory=dict)

    @asynccontextmanager
    async def __call__(self, url: str):
        site = site_of(url)
        gated = self.sites.setdefault(site, GatedSite(asyncio.Semaphore(self.per_site)))
        gated.users += 1
        try:
            async with gated.slots:
                now = time.monotonic()
                start = max(now, gated.next_start)
                gated.next_start = start + self.gap
                await asyncio.sleep(start - now)
                yield
        finally:
            gated.users -= 1
            if gated.users == 0:
                del self.sites[site]
