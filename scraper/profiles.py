"""What the scraper has learned about each site, so it can skip steps it knows will fail.

A profile says whether plain requests to the site get blocked, and which parsers only find their
data once the browser has run the page's JavaScript. It is learned from real outcomes, kept in
Redis for a week (shared by every machine, and kept across restarts), and mirrored in memory.
Parsers can mark their site as blocking, which is the starting guess until something is learned.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from . import cache
from .blocking import looks_blocked
from .http_fetch import fetch_page
from .parsers import PARSERS, Parser, parse_page

PROFILE_SECONDS = 7 * 24 * 3600  # a site nobody scrapes for a week is forgotten
PROFILE_MEMORY_SECONDS = 60  # how long the memory copy is used before Redis is asked again
MAX_PROFILES = 1000
RECHECK_SECONDS = 24 * 3600  # how often a site sent past the plain route gets one plain try again

logger = logging.getLogger(__name__)


@dataclass
class SiteProfile:
    blocks_plain: bool = False  # plain requests without bot-check cookies get blocked
    render: set[str] = field(default_factory=set)  # parsers whose data needs the browser on this site
    checked_at: float = 0.0  # wall clock: when the plain route was last tried
    seeded: bool = False  # True while this is only a parser's guess, nothing learned yet


SEEDED_SITES = {p.host for p in PARSERS if p.blocks_plain}

# site -> (loaded at, learned profile or None for "nothing learned", expires at); both times monotonic.
_memory: dict[str, tuple[float, SiteProfile | None, float]] = {}
_memory_lock = threading.Lock()


def clear_memory() -> None:
    """Forget everything this process holds in memory (used by tests)."""
    with _memory_lock:
        _memory.clear()
    with _recheck_lock:
        rechecks_in_flight.clear()


def _key(site: str) -> str:
    return f"site:{site}"


def _from_json(data: dict | None) -> SiteProfile | None:
    if data is None:
        return None
    blocks, render, checked = data.get("b"), data.get("r"), data.get("c")
    if not isinstance(blocks, bool) or not isinstance(render, list) or not all(isinstance(r, str) for r in render):
        return None
    if isinstance(checked, bool) or not isinstance(checked, int | float):
        return None
    return SiteProfile(blocks, set(render), float(checked))


def _remember(site: str, profile: SiteProfile | None, expires: float) -> None:
    with _memory_lock:
        now = time.monotonic()
        _memory[site] = (now, profile, expires)
        if len(_memory) > MAX_PROFILES:
            oldest = sorted(_memory, key=lambda s: _memory[s][0])[: len(_memory) // 2]
            for stale in oldest:
                del _memory[stale]


def _learned(site: str) -> SiteProfile | None:
    """The learned profile, from memory while it is fresh, otherwise from Redis. Without Redis the
    memory copy is the only one, so it is kept until the profile itself expires."""
    now = time.monotonic()
    with _memory_lock:
        entry = _memory.get(site)
    if entry is not None:
        loaded, profile, expires = entry
        if now - loaded <= PROFILE_MEMORY_SECONDS or (not cache.redis_available() and now < expires):
            return profile
    if not cache.redis_available():
        return None
    profile, expires = _from_json(cache.get_json(_key(site))), now + PROFILE_SECONDS
    if profile is None and entry is not None and entry[1] is not None and now < entry[2]:
        # Redis has nothing (it was down when this was learned) or the read just failed. Nothing
        # ever deletes a profile from Redis early, so the memory copy is still the latest word.
        profile, expires = entry[1], entry[2]
    _remember(site, profile, expires)
    return profile


def get_profile(site: str) -> SiteProfile:
    """What is known about the site, as a copy the caller may change."""
    profile = _learned(site)
    if profile is not None:
        return replace(profile, render=set(profile.render))
    if site in SEEDED_SITES:
        return SiteProfile(blocks_plain=True, checked_at=time.time(), seeded=True)
    return SiteProfile()


def save_profile(site: str, profile: SiteProfile) -> None:
    stored = replace(profile, render=set(profile.render), seeded=False)
    _remember(site, stored, time.monotonic() + PROFILE_SECONDS)
    data = {"b": stored.blocks_plain, "r": sorted(stored.render), "c": stored.checked_at}
    cache.set_json(_key(site), data, PROFILE_SECONDS)


def _learn(site: str, **changes) -> None:
    profile = get_profile(site)
    for name, value in changes.items():
        setattr(profile, name, value)
    profile.checked_at = time.time()
    save_profile(site, profile)


def learn_blocked(site: str) -> None:
    logger.info("Learned that %s blocks plain requests", site)
    _learn(site, blocks_plain=True)


def learn_not_blocked(site: str) -> None:
    logger.info("Learned that %s answers plain requests", site)
    _learn(site, blocks_plain=False)


def learn_needs_render(site: str, parser_name: str) -> None:
    logger.info("Learned that %s on %s needs the browser to render", parser_name, site)
    _learn(site, render=get_profile(site).render | {parser_name})


def learn_renders_plain(site: str, parser_name: str) -> None:
    logger.info("Learned that %s on %s works from the plain page", parser_name, site)
    _learn(site, render=get_profile(site).render - {parser_name})


# ---- Daily re-check --------------------------------------------------------------------------
# A site that stopped blocking, or a page that stopped needing JavaScript, would otherwise keep
# paying for the browser for a week. Once a day one plain request finds out, in the background,
# so no caller waits for it.

recheck_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recheck")
rechecks_in_flight: set[str] = set()
_recheck_lock = threading.Lock()


def maybe_recheck(site: str, url: str, parser: Parser | None = None) -> None:
    """After a request the profile sent past the plain route: if that route was last tried more
    than a day ago, try it again in the background. Without a parser the question is "does the
    site still block?", with one it is "does this parser still need the browser?"."""
    profile = get_profile(site)
    if profile.seeded or time.time() - profile.checked_at <= RECHECK_SECONDS:
        return
    if not (parser.name in profile.render if parser else profile.blocks_plain):
        return
    with _recheck_lock:
        if site in rechecks_in_flight:
            return
        rechecks_in_flight.add(site)
    # Claim the check first, so other requests and other machines don't start their own.
    profile.checked_at = time.time()
    save_profile(site, profile)
    recheck_pool.submit(run_recheck, site, url, parser)


def run_recheck(site: str, url: str, parser: Parser | None) -> None:
    try:
        page = fetch_page(url)
        if looks_blocked(page):
            return
        if parser is None:
            learn_not_blocked(site)
        else:
            parse_page(parser, page)
            learn_renders_plain(site, parser.name)
    except Exception as e:  # a failed check changes nothing; the route stays as it is
        logger.info("Re-check of %s found nothing new: %s", url, e)
    finally:
        with _recheck_lock:
            rechecks_in_flight.discard(site)
