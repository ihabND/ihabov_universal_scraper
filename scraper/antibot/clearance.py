"""Cookies from passed bot checks, kept per API key and site so plain HTTP can reuse them."""

import logging
import time
from dataclasses import asdict, dataclass

from ..store.cache import get_json, set_json

CLEARANCE_SECONDS = 4 * 3600
PLAIN_RETRY_SECONDS = 3600  # after plain HTTP was blocked even with the cookies, skip it this long
# The browser headers bot-check cookies are usually bound to; sent along with the cookies.
CLEARANCE_HEADERS = ("user-agent", "accept-language", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")
# Akamai Bot Manager's cookies are bound to the browser that earned them: replayed over plain HTTP they get
# flagged, and a browser that then starts with them is blocked too.
UNREPLAYABLE_COOKIES = {"_abck", "bm_sz", "ak_bmsc", "bm_sv"}

logger = logging.getLogger(__name__)


@dataclass
class Clearance:
    cookies: list[dict]
    headers: dict[str, str]
    expires: float = 0.0
    plain_blocked_at: float = 0.0

    @property
    def plain_ok(self) -> bool:
        return time.time() - self.plain_blocked_at >= PLAIN_RETRY_SECONDS


def record_name(owner: str, site: str) -> str:
    return f"clearance:{owner}:{site}"  # owner is a hash of the API key, never the key


async def get_clearance(owner: str | None, site: str) -> Clearance | None:
    record = await get_json(record_name(owner, site)) if owner else None
    try:
        clearance = Clearance(**record) if record else None
    except TypeError:
        return None
    if clearance is None:
        return None
    now = time.time()
    clearance.cookies = [c for c in clearance.cookies if not 0 < c.get("expires", -1) <= now]
    return clearance if clearance.cookies else None


async def store(owner: str, site: str, clearance: Clearance) -> None:
    ttl = int(clearance.expires - time.time())
    if ttl > 0:
        await set_json(record_name(owner, site), asdict(clearance), ttl)


async def save_clearance(owner: str, site: str, earned: Clearance) -> None:
    if any(c["name"] in UNREPLAYABLE_COOKIES for c in earned.cookies):
        return
    old = await get_clearance(owner, site)
    earned.expires = time.time() + CLEARANCE_SECONDS
    earned.plain_blocked_at = old.plain_blocked_at if old else 0.0
    await store(owner, site, earned)
    logger.info("Kept %d bot-check cookies for %s", len(earned.cookies), site)


async def mark_plain_blocked(owner: str, site: str, clearance: Clearance) -> None:
    clearance.plain_blocked_at = time.time()
    await store(owner, site, clearance)
