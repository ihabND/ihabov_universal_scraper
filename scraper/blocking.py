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
# After plain HTTP failed with a key's cookies, renewed cookies skip plain HTTP this long, then get a fresh try.
HTTP_RETRY_SECONDS = 3600
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
    http_checked: float = 0.0  # wall clock: when http_works was last set


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
    http_checked = data.get("http_checked", 0.0)
    if not isinstance(cookies, list) or not all(isinstance(c, dict) for c in cookies):
        return None
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        return None
    if http_works not in (True, False, None):
        return None
    if any(isinstance(n, bool) or not isinstance(n, int | float) for n in (expires, http_checked)):
        return None
    live = [c for c in cookies if not (isinstance(c.get("expires"), int | float) and 0 < c["expires"] <= now)]
    if not live or expires <= now:
        return None
    return Clearance(live, headers, float(expires), http_works, float(http_checked))


def _store(owner: str, site: str, clearance: Clearance) -> None:
    ttl = int(clearance.expires - time.time())
    if ttl > 0:
        data = {
            "cookies": clearance.cookies,
            "headers": clearance.headers,
            "expires": clearance.expires,
            "http_works": clearance.http_works,
            "http_checked": clearance.http_checked,
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
    now = time.time()
    # If plain HTTP recently failed with this browser's cookies, don't try it again on every renewal.
    # After HTTP_RETRY_SECONDS the new cookies get a fresh try: the old ones may simply have gone stale.
    failed_recently = (
        old is not None
        and old.http_works is False
        and old.headers == headers
        and now - old.http_checked < HTTP_RETRY_SECONDS
    )
    if failed_recently:
        clearance = Clearance(cookies, headers, now + CLEARANCE_SECONDS, False, old.http_checked)
    else:
        clearance = Clearance(cookies, headers, now + CLEARANCE_SECONDS)
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
        clearance.http_checked = time.time()
    _store(owner, site, clearance)
