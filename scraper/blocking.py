"""Recognising bot protection, and remembering what helped get past it."""

import logging
import threading
import time
from dataclasses import dataclass

from . import cache
from .page import Page

BLOCKED_STATUS_CODES = {202, 403, 429, 503}
CHALLENGE_MARKERS = (b"awswaf", b"_cf_chl_opt", b"<title>Just a moment...</title>", b"captcha-delivery.com")
CHALLENGE_PAGE_MAX_BYTES = 150 * 1024
CLEARANCE_SECONDS = 4 * 3600  # how long cookies from a passed bot check are kept for reuse
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
    are bound to. Kept per API key and site, in memory and in Redis, and never used for another
    key, so one caller's cookies are never sent on another caller's request."""

    cookies: list[dict]
    headers: dict[str, str]
    expires: float  # wall clock, since the record is shared between processes
    http_works: bool | None = None  # None: not tried yet. False: plain HTTP was still blocked with them.


clearances: dict[tuple[str, str], Clearance] = {}
clearances_lock = threading.Lock()


def clearance_key(owner: str, site: str) -> str:
    # owner is already a hash of the API key (quota.key_id), never the key itself.
    return f"clearance:{owner}:{site}"


def _from_json(data: dict | None, now: float) -> Clearance | None:
    """A clearance read back from Redis, without the cookies that have expired on their own."""
    if data is None:
        return None
    cookies, headers, expires, http_works = (data.get(k) for k in ("cookies", "headers", "expires", "http_works"))
    if not isinstance(cookies, list) or not all(isinstance(c, dict) for c in cookies):
        return None
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        return None
    if isinstance(expires, bool) or not isinstance(expires, int | float) or http_works not in (True, False, None):
        return None
    live = [c for c in cookies if not (isinstance(c.get("expires"), int | float) and 0 < c["expires"] <= now)]
    if not live or expires <= now:
        return None
    return Clearance(live, headers, float(expires), http_works)


def _store(owner: str, site: str, clearance: Clearance) -> None:
    ttl = int(clearance.expires - time.time())
    if ttl > 0:
        data = {
            "cookies": clearance.cookies,
            "headers": clearance.headers,
            "expires": clearance.expires,
            "http_works": clearance.http_works,
        }
        cache.set_json(clearance_key(owner, site), data, ttl)


def get_clearance(owner: str | None, site: str) -> Clearance | None:
    if not owner:
        return None
    now = time.time()
    with clearances_lock:
        clearance = clearances.get((owner, site))
        if clearance is not None:
            if clearance.expires > now:
                return clearance
            del clearances[(owner, site)]
    clearance = _from_json(cache.get_json(clearance_key(owner, site)), now)
    if clearance is not None:
        with clearances_lock:
            clearances[(owner, site)] = clearance
    return clearance


def save_clearance(owner: str | None, site: str, cookies: list[dict], headers: dict[str, str]) -> None:
    if not owner or not cookies:
        return
    old = get_clearance(owner, site)
    # If plain HTTP already failed with this browser's cookies, don't try it again on every renewal.
    http_works = False if old is not None and old.http_works is False and old.headers == headers else None
    now = time.time()
    clearance = Clearance(cookies, headers, now + CLEARANCE_SECONDS, http_works)
    with clearances_lock:
        clearances[(owner, site)] = clearance
        if len(clearances) > MAX_CLEARANCES:
            for key in [k for k, c in clearances.items() if c.expires <= now]:
                del clearances[key]
            for key in sorted(clearances, key=lambda k: clearances[k].expires)[: len(clearances) - MAX_CLEARANCES]:
                del clearances[key]
    _store(owner, site, clearance)
    logger.info("Kept %d bot-check cookies for %s", len(cookies), site)


def note_http_result(owner: str, site: str, clearance: Clearance, worked: bool) -> None:
    with clearances_lock:
        clearance.http_works = worked
    _store(owner, site, clearance)
