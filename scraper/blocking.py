"""Recognising bot protection, and remembering what helped get past it."""

import logging
import threading
import time
from dataclasses import dataclass

from .page import Page

BLOCKED_STATUS_CODES = {202, 403, 429, 503}
CHALLENGE_MARKERS = (b"awswaf", b"_cf_chl_opt", b"<title>Just a moment...</title>", b"captcha-delivery.com")
CHALLENGE_PAGE_MAX_BYTES = 150 * 1024
CLEARANCE_SECONDS = 20 * 60  # how long cookies from a passed bot check are kept for reuse
MAX_CLEARANCES = 1000
# The browser headers that bot-check cookies are usually bound to; sent along with those cookies.
CLEARANCE_HEADERS = ("user-agent", "accept-language", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")

logger = logging.getLogger(__name__)


def has_challenge(content: bytes) -> bool:
    return len(content) <= CHALLENGE_PAGE_MAX_BYTES and any(m in content for m in CHALLENGE_MARKERS)


def looks_blocked(page: Page) -> bool:
    return page.status_code in BLOCKED_STATUS_CODES or has_challenge(page.content)


# Which sites block plain requests is remembered in profiles.py.

# ---- Cookies from passed bot checks --------------------------------------------------------


@dataclass
class Clearance:
    """Cookies a browser earned by getting past a site's bot check, with the browser headers they
    are bound to. Kept per API key and site, in this process's memory only, and never used for
    another key, so one caller's cookies are never sent on another caller's request."""

    cookies: list[dict]
    headers: dict[str, str]
    expires: float
    http_works: bool | None = None  # None: not tried yet. False: plain HTTP was still blocked with them.


clearances: dict[tuple[str, str], Clearance] = {}
clearances_lock = threading.Lock()


def get_clearance(owner: str | None, site: str) -> Clearance | None:
    if not owner:
        return None
    with clearances_lock:
        clearance = clearances.get((owner, site))
        if clearance is not None and clearance.expires <= time.monotonic():
            del clearances[(owner, site)]
            return None
        return clearance


def save_clearance(owner: str | None, site: str, cookies: list[dict], headers: dict[str, str]) -> None:
    if not owner or not cookies:
        return
    with clearances_lock:
        now = time.monotonic()
        old = clearances.get((owner, site))
        # If plain HTTP already failed with this browser's cookies, don't try it again on every renewal.
        http_works = False if old is not None and old.http_works is False and old.headers == headers else None
        clearances[(owner, site)] = Clearance(cookies, headers, now + CLEARANCE_SECONDS, http_works)
        if len(clearances) > MAX_CLEARANCES:
            for key in [k for k, c in clearances.items() if c.expires <= now]:
                del clearances[key]
            for key in sorted(clearances, key=lambda k: clearances[k].expires)[: len(clearances) - MAX_CLEARANCES]:
                del clearances[key]
    logger.info("Kept %d bot-check cookies for %s", len(cookies), site)


def note_http_result(clearance: Clearance, worked: bool) -> None:
    with clearances_lock:
        clearance.http_works = worked
