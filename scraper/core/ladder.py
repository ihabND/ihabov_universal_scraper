"""Choosing a fetcher: start with the cheapest one the site is known to need, move on when a fetcher is
blocked or its page lacks the parser's data, and remember what worked."""

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import nullcontext

from ..antibot.clearance import get_clearance, mark_plain_blocked, save_clearance
from ..fetchers.base import Fetcher, FetchRequest
from ..net.urls import site_of
from ..parsers import Parsed, Parser, parse_page, ready_check, seed_tier
from ..store.profiles import SiteProfile, get_profile, save_profile
from .errors import BlockedError, BrowserBusyError, BrowserDisabledError, FetchError, ParseError
from .page import Page

# Failures another fetcher might not have. Anything else (an internal address, a PDF, a page that is
# too large) would be the same with every fetcher.
RECOVERABLE = (FetchError, BrowserBusyError, BrowserDisabledError)

logger = logging.getLogger(__name__)


async def fetch(
    url: str,
    tiers: list[Fetcher],
    *,
    parser: Parser | None = None,
    owner: str | None = None,
    gate: Callable | None = None,
    queue_timeout: float = 15,
) -> tuple[Page, Parsed | None]:
    started, site = time.monotonic(), site_of(url)
    profile = await get_profile(site) or SiteProfile(tier=seed_tier(site))
    remembered = next((i for i, tier in enumerate(tiers) if tier.name == profile.tier), 0)
    clearance = await get_clearance(owner, site)
    needs_render = parser is not None and parser.name in profile.render
    start = remembered
    if needs_render:
        start = max(start, next((i for i, tier in enumerate(tiers) if tier.renders), start))
    elif clearance and clearance.plain_ok:
        start = 0  # plain HTTP with this key's bot-check cookies beats every other route
    req = FetchRequest(url, owner, clearance, ready_check(parser, url) if parser else None, queue_timeout=queue_timeout)
    blocked = unparsed = None
    errors = []
    # More capable fetchers first, then the cheaper ones in case the remembered route fails.
    for i in [*range(start, len(tiers)), *reversed(range(start))]:
        tier = tiers[i]
        try:
            async with gate(url) if gate else nullcontext():
                page = await tier.fetch(req)
        except RECOVERABLE as e:
            logger.info("%s: %s failed: %s", site, tier.name, e)
            errors.append(e)
            continue
        if page.blocked:
            if not tier.renders and clearance and clearance.plain_ok:
                await mark_plain_blocked(owner, site, clearance)
            blocked, req.after_block = page, True
            continue
        parsed = None
        if parser:
            try:
                parsed = await asyncio.to_thread(parse_page, parser, page)
            except ParseError:
                if tier.renders or not page.ok:  # an error page, or a page that already ran its JavaScript
                    raise
                unparsed = page
                continue
        learned = SiteProfile(profile.tier, list(profile.render))
        if blocked or i < start == remembered:
            learned.tier = tier.name
        if parser and unparsed and tier.renders:
            learned.render = sorted({*learned.render, parser.name})
        if parser and needs_render and not tier.renders:
            learned.render.remove(parser.name)
        if learned != profile:
            logger.info("%s: learned %s", site, learned)
            await save_profile(site, learned)
        if page.earned and owner:
            await save_clearance(owner, site_of(page.url), page.earned)
        logger.info("%s: %s in %.2f s", site, tier.name, time.monotonic() - started)
        return page, parsed
    busy = next((e for e in errors if isinstance(e, BrowserBusyError)), None)
    if blocked and busy:
        raise busy  # the browser never got its turn, so this is "try again soon", not a block
    if blocked and parser:
        hint = "every way of fetching this page" if any(t.renders for t in tiers) else "it; try browser=auto"
        raise BlockedError(f"The site's bot protection blocked {hint}.")
    if blocked:
        return blocked, None
    if errors:
        raise errors[0]
    raise ParseError("Couldn't find the expected data on this page.")
