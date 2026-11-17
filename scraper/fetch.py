"""Choosing how to fetch a page: plain HTTP first, the browser only when it is needed."""

import logging
import time
from collections.abc import Callable
from functools import partial
from typing import Literal

from .blocking import get_clearance, looks_blocked, note_http_result
from .browser import BROWSER_ENABLED, BROWSER_QUEUE_TIMEOUT_SECONDS, fetch_with_browser
from .errors import FetchError, ScrapeError
from .http_fetch import fetch_page
from .page import Page
from .profiles import get_profile, learn_blocked, learn_not_blocked, maybe_recheck
from .urls import site_of

logger = logging.getLogger(__name__)


def log_route(site: str, route: str, started: float) -> None:
    logger.info("%s: %s in %.2f s", site, route, time.monotonic() - started)


def fetch(
    url: str,
    browser: Literal["auto", "never", "always"] = "auto",
    *,
    owner: str | None = None,
    ready: Callable[[bytes], bool] | None = None,
    queue_timeout: float = BROWSER_QUEUE_TIMEOUT_SECONDS,
) -> Page:
    """Fetch a page the cheapest way that works.

    owner:  who is asking (a hash of the API key). Bot-check cookies are kept and reused per owner.
    ready:  JSON mode's test for "the data is there", which lets the browser stop early.
    """
    started = time.monotonic()
    site = site_of(url)
    in_browser = partial(fetch_with_browser, owner=owner, ready=ready, queue_timeout=queue_timeout)
    if browser == "always":
        page = in_browser(url)
        log_route(site, "browser (always)", started)
        return page
    use_fallback = browser == "auto" and BROWSER_ENABLED
    clearance = get_clearance(owner, site)
    if clearance is not None and clearance.http_works is False:
        clearance = None  # these cookies didn't get plain requests through; the browser still starts with them
    # Cookies from a passed bot check come first: a plain request with them is far faster than the browser.
    if use_fallback and clearance is None and get_profile(site).blocks_plain:
        page = fetch_known_blocked(url, site, in_browser)
        if page.fetched_with == "browser":
            log_route(site, "browser (profile)", started)
            maybe_recheck(site, url)
        else:
            log_route(site, "plain", started)
        return page
    try:
        page = fetch_page(url, clearance)
    except FetchError as e:
        if not use_fallback:
            raise
        logger.info("HTTP fetch failed for %s (%s), trying the browser", url, e)
        page = in_browser(url)
        log_route(site, "plain, then browser", started)
        return page
    blocked = looks_blocked(page)
    if clearance is not None:
        note_http_result(clearance, not blocked)
    if use_fallback and blocked:
        logger.info("HTTP fetch of %s looks blocked (status %d), trying the browser", page.url, page.status_code)
        page = in_browser(page.url, after_block=True)
        learn_blocked(site)
        log_route(site, "plain, then browser", started)
        return page
    log_route(site, "plain+cookies" if clearance is not None else "plain", started)
    return page


def fetch_known_blocked(url: str, site: str, in_browser: Callable[..., Page]) -> Page:
    """For a site known to block plain requests: try the browser first, which skips an HTTP attempt
    that would most likely be blocked again. If the browser fails, the plain fetch still runs, so
    the outcome is never worse than trying HTTP first: a page that isn't blocked any more is
    returned, and otherwise the browser's error stands, as it would have."""
    logger.info("%s is known to block plain requests, going straight to the browser", site)
    try:
        return in_browser(url, after_block=True)
    except ScrapeError as browser_error:
        try:
            page = fetch_page(url)
        except FetchError:
            raise browser_error from None
        # Any other ScrapeError (internal address, too large, not HTML) propagates, as it would
        # have from an HTTP-first attempt.
        if looks_blocked(page):
            raise browser_error from None
        logger.info("%s answered a plain request again", site)
        learn_not_blocked(site)
        return page
