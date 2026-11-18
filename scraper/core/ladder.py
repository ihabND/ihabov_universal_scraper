"""Choosing a fetcher: start with the cheapest one the site is known to need, move on when a fetcher is
blocked or its page lacks the parser's data, and remember what worked."""

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import nullcontext

from ..antibot.clearance import get_clearance, mark_plain_blocked, save_clearance
from ..antibot.detect import challenge_of
from ..fetchers.base import Fetcher, FetchRequest
from ..net.urls import site_of
from ..parsers import Parsed, Parser, parse_page, ready_check
from ..store import strategies
from .errors import BlockedError, BrowserBusyError, BrowserDisabledError, FetchError, ParseError, ScrapeError
from .page import Page, looks_empty
from .strategy import Strategy

# Failures another fetcher might not have. Anything else (an internal address, a PDF, a page that is
# too large) would be the same with every fetcher.
RECOVERABLE = (FetchError, BrowserBusyError, BrowserDisabledError)

HTML_RENDER = "html"  # the render-memory name for HTML-mode pages that need JavaScript
logger = logging.getLogger(__name__)


async def fetch(
    url: str,
    tiers: list[Fetcher],
    *,
    strategy: Strategy,
    parser: Parser | None = None,
    owner: str | None = None,
    gate: Callable | None = None,
    queue_timeout: float = 15,
) -> tuple[Page, Parsed | None]:
    started, site = time.monotonic(), site_of(url)
    tiers = arranged(tiers, strategy.fingerprints)
    remembered = next((i for i, tier in enumerate(tiers) if tier.name == strategy.start), 0)
    clearance = await get_clearance(owner, site)
    render_name = parser.name if parser else HTML_RENDER
    needs_render = render_name in strategy.render
    start = remembered
    if needs_render:
        start = max(start, next((i for i, tier in enumerate(tiers) if tier.renders), start))
    elif clearance and clearance.plain_ok:
        start = 0  # plain HTTP with this key's bot-check cookies beats every other route
    # Cookies that plain HTTP was already blocked with may be burned, so no fetcher gets them.
    usable = clearance if clearance and clearance.plain_ok else None
    req = FetchRequest(url, owner, usable, ready_check(parser, url) if parser else None, queue_timeout=queue_timeout)
    blocked = unparsed = None
    why = ""  # what made the scraper move past a fetcher, kept as the evidence for what it learns
    errors: list[ScrapeError] = []  # a parser's complaint about a page goes in here too
    # More capable fetchers first, then the cheaper ones in case the remembered route fails.
    for i in [*range(start, len(tiers)), *reversed(range(start))]:
        tier = tiers[i]
        try:
            async with gate(url) if gate else nullcontext():
                page = await tier.fetch(req)
        except RECOVERABLE as e:
            logger.info("%s: %s failed: %s", site, tier.name, e)
            errors.append(e)
            why = f"{tier.name} failed: {e}"
            continue
        if page.blocked:
            if not tier.renders and req.clearance:
                await mark_plain_blocked(owner, site, req.clearance)
                req.clearance = None
            blocked, req.after_block = page, True
            vendor = challenge_of(page.content)
            why = f"{tier.name} got {page.status_code}" + (f" ({vendor} check)" if vendor else "")
            continue
        parsed = None
        if parser:
            try:
                parsed = await asyncio.to_thread(parse_page, parser, page)
            except ParseError as e:
                if tier.renders or not page.ok:  # an error page, or a page that already ran its JavaScript
                    raise
                unparsed = page
                errors.append(e)
                why = f"{tier.name}'s page lacked the data"
                continue
        elif not tier.renders and page.ok and await asyncio.to_thread(looks_empty, page):
            unparsed = page  # built by JavaScript, so a fetcher that runs it may get the content
            why = f"{tier.name}'s page was nearly empty"
            continue
        if (blocked or i < start == remembered) and tier.name != strategy.start:
            strategies.learn(site, "start", tier.name, why)
            logger.info("%s: learned start=%s (%s)", site, tier.name, why)
        render = set(strategy.render)
        if unparsed and tier.renders and (parser or not await asyncio.to_thread(looks_empty, page)):
            render.add(render_name)
        if needs_render and not tier.renders:
            render.discard(render_name)
            why = f"{tier.name}'s page had the data"
        if render != set(strategy.render):
            strategies.learn(site, "render", sorted(render), why)
            logger.info("%s: learned render=%s (%s)", site, sorted(render), why)
        if page.earned and owner:
            await save_clearance(owner, site_of(page.url), page.earned)
        logger.info("%s: %s in %.2f s", site, tier.name, time.monotonic() - started)
        return page, parsed
    if unparsed and not parser:
        return unparsed, None  # the plain page is the best there is
    busy = next((e for e in errors if isinstance(e, BrowserBusyError)), None)
    if busy and (blocked or unparsed):
        raise busy  # the browser never got its turn, so this is "try again soon", not a block
    if blocked and parser:
        hint = "every way of fetching this page" if any(t.renders for t in tiers) else "it; try browser=auto"
        raise BlockedError(f"The site's bot protection blocked {hint}.")
    if blocked:
        return blocked, None
    raise next((e for e in errors if not isinstance(e, ParseError)), errors[0])


def arranged(tiers: list[Fetcher], fingerprints: list[str]) -> list[Fetcher]:
    """Plain fetchers in the site's fingerprint order (ones it doesn't list are left out), then the rest."""
    order = {fingerprint: i for i, fingerprint in enumerate(fingerprints)}
    kept = [t for t in tiers if getattr(t, "impersonate", None) in (None, *order)]
    return sorted(kept, key=lambda t: (t.renders, order.get(getattr(t, "impersonate", None), 0)))
