"""Scraping one URL or a batch: cache lookups, fetching, parsing, and building the response."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext

from bs4 import BeautifulSoup

from .browser import BROWSER_ENABLED, BROWSER_QUEUE_TIMEOUT_SECONDS, fetch_with_browser
from .cache import get_cached, get_cached_page, set_cached, set_cached_page
from .errors import InvalidURLError, ParseError, ScrapeError, UnsupportedSiteError
from .fetch import fetch
from .page import Page
from .parsers import PARSERS, Parser, find_parser, parse_page, ready_check
from .profiles import get_profile, learn_needs_render, learn_renders_plain, maybe_recheck
from .schemas import BatchItem, BatchResponse, BrowserOption, Mode, ScrapeResponse
from .urls import normalize_url, site_of

BATCH_WORKERS = 8  # URLs fetched at once across all batches
BATCH_PER_SITE = 4  # at most this many requests to one site at the same time
BATCH_SITE_GAP_SECONDS = 0.25  # and at least this long between starting them
BATCH_BROWSER_QUEUE_SECONDS = 120  # batch items wait longer for a free browser than single requests

logger = logging.getLogger(__name__)


def clean_html_response(response: Page) -> str:
    return BeautifulSoup(response.content, "lxml").prettify()


# ---- Page cache helpers ----------------------------------------------------------------------


def cached_page(url: str, browser: BrowserOption, mode: Mode) -> Page | None:
    entry = get_cached_page(url, browser)
    if entry is None:
        return None
    fields, content = entry
    try:
        page = Page(fields["url"], fields["status_code"], content, fields["fetched_with"], fields["complete"])
    except (KeyError, TypeError):
        return None
    # A browser page cut short for JSON mode may lack content that loads later: HTML mode refetches.
    if mode == "html" and not page.complete:
        return None
    logger.info("Page cache hit for %s", url)
    return page


def remember_page(url: str, browser: BrowserOption, page: Page) -> None:
    fields = {
        "url": page.url,
        "status_code": page.status_code,
        "fetched_with": page.fetched_with,
        "complete": page.complete,
    }
    set_cached_page(url, browser, fields, page.content, page.ok)


# ---- Scraping ------------------------------------------------------------------------------


def scrape(
    url: str,
    mode: Mode = "html",
    browser: BrowserOption = "auto",
    *,
    owner: str | None = None,
    gate=None,
    queue_timeout: float = BROWSER_QUEUE_TIMEOUT_SECONDS,
) -> ScrapeResponse:
    """Scrape one URL. `gate`, used by batches, wraps every network fetch (not cache hits) to keep
    the number of requests per site in check."""
    normalized = normalize_url(url)
    if normalized is None:
        raise InvalidURLError("Invalid URL, only HTTP and HTTPS.")

    cached = get_cached(normalized, mode, browser)
    if cached is not None:
        logger.info("Cache hit for %s", normalized)
        return ScrapeResponse.model_validate_json(cached)

    parser: Parser | None = None
    if mode == "json":
        parser = find_parser(normalized)
        if parser is None:
            supported = ", ".join(dict.fromkeys(p.host for p in PARSERS))
            raise UnsupportedSiteError(f"JSON isn't available for this page. Supported sites: {supported}.")
    ready = ready_check(parser, normalized) if parser else None

    def gated():
        return gate(normalized) if gate else nullcontext()

    site = site_of(normalized)
    browser_error: ScrapeError | None = None  # set when the remembered browser route failed
    page = cached_page(normalized, browser, mode)
    if page is None:
        # A parser known to need JavaScript on this site skips the plain page it can't read.
        render_first = parser is not None and browser == "auto" and BROWSER_ENABLED
        if render_first and parser.name in get_profile(site).render:
            try:
                with gated():
                    page = fetch_with_browser(normalized, owner=owner, ready=ready, queue_timeout=queue_timeout)
                maybe_recheck(site, normalized, parser)
            except ScrapeError as e:
                # The memory may be wrong: the plain page still gets its try, as it would have without it.
                logger.info("The browser failed on %s (%s), trying the plain page", normalized, e)
                browser_error = e
        if page is None:
            route = "never" if browser_error else browser  # after a browser failure, plain only
            try:
                with gated():
                    page = fetch(normalized, route, owner=owner, ready=ready, queue_timeout=queue_timeout)
            except ScrapeError:
                if browser_error is not None:
                    raise browser_error from None
                raise
        remember_page(normalized, browser, page)
    if parser:
        try:
            parsed = parse_page(parser, page)
            if browser_error is not None:
                learn_renders_plain(site, parser.name)
        except ParseError:
            if browser_error is not None:
                raise browser_error from None
            # Only retry with the browser when it could help: an error page (404, 500) will
            # be the same error page in a browser, so don't spend a browser run on it.
            if page.fetched_with == "browser" or browser == "never" or not BROWSER_ENABLED or not page.ok:
                raise
            logger.info("Parsing %s failed, retrying with the browser", page.url)
            with gated():
                page = fetch_with_browser(page.url, owner=owner, ready=ready, queue_timeout=queue_timeout)
            remember_page(normalized, browser, page)
            parsed = parse_page(parser, page)
            learn_needs_render(site, parser.name)
    result = ScrapeResponse(url=normalized, status_code=page.status_code, ok=page.ok, fetched_with=page.fetched_with)
    if parser:
        result.parsed = parsed
    else:
        result.content = clean_html_response(page)
    set_cached(normalized, mode, browser, result.model_dump_json(), result.ok)
    return result


# ---- Batches -------------------------------------------------------------------------------


class SiteGate:
    """Politeness for batches: at most `per_site` requests to one site at a time, started at least
    `gap` seconds apart. Shared by all batches, so two batches to one site don't double the load."""

    def __init__(self, per_site: int, gap: float):
        self.per_site, self.gap = per_site, gap
        self.lock = threading.Lock()
        self.slots: dict[str, threading.BoundedSemaphore] = {}
        self.next_start: dict[str, float] = {}

    @contextmanager
    def __call__(self, url: str):
        site = site_of(url)
        with self.lock:
            slot = self.slots.setdefault(site, threading.BoundedSemaphore(self.per_site))
        with slot:
            with self.lock:
                now = time.monotonic()
                start = max(now, self.next_start.get(site, 0.0))
                self.next_start[site] = start + self.gap
                if len(self.next_start) > 1000:  # forget sites whose spacing has long passed
                    for old in [s for s, t in self.next_start.items() if t < now]:
                        del self.next_start[old]
            if start > now:
                time.sleep(start - now)
            yield


site_gate = SiteGate(BATCH_PER_SITE, BATCH_SITE_GAP_SECONDS)
batch_pool = ThreadPoolExecutor(max_workers=BATCH_WORKERS, thread_name_prefix="batch")


def scrape_item(url: str, mode: Mode, browser: BrowserOption, owner: str) -> BatchItem:
    try:
        result = scrape(url, mode, browser, owner=owner, gate=site_gate, queue_timeout=BATCH_BROWSER_QUEUE_SECONDS)
        return BatchItem(url=url, status=200, result=result)
    except ScrapeError as e:
        return BatchItem(url=url, status=e.status_code, detail=str(e))
    except Exception:  # one broken URL must not sink the whole batch
        logger.exception("Batch item %s failed", url)
        return BatchItem(url=url, status=500, detail="Internal error while scraping this URL.")


def run_batch(urls: list[str], mode: Mode, browser: BrowserOption, owner: str) -> BatchResponse:
    """Scrape several URLs at once. Every URL is handled exactly as a single scrape would handle it;
    failures are reported per URL instead of failing the whole batch."""
    # The same page sent twice is scraped once.
    futures = {}
    for url in urls:
        key = normalize_url(url) or url
        if key not in futures:
            futures[key] = batch_pool.submit(scrape_item, url, mode, browser, owner)
    results = []
    for url in urls:
        item = futures[normalize_url(url) or url].result()
        results.append(item if item.url == url else item.model_copy(update={"url": url}))
    succeeded = sum(1 for r in results if r.status == 200)
    return BatchResponse(results=results, succeeded=succeeded, failed=len(results) - succeeded)
