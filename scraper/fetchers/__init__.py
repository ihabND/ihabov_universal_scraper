import logging
from importlib.util import find_spec

from ..net.guard import EgressGuard
from ..settings import settings
from .base import Fetcher
from .browser import BrowserFetcher
from .http import HttpFetcher

logger = logging.getLogger(__name__)


def build_tiers(guard: EgressGuard) -> list[Fetcher]:
    """The fetchers, cheapest first. A new tier (a stealth browser, an unblocker API) is added here."""
    tiers: list[Fetcher] = [
        HttpFetcher(guard),
        # Sites that block curl's Chrome fingerprint often let its Safari one through (7 of 100 in the
        # benchmark). No retries after the Chrome tier, which already retried temporary errors.
        HttpFetcher(guard, name="http-safari", impersonate="safari", retries=0),
    ]
    if settings.browser_enabled:
        tiers.append(
            BrowserFetcher(
                guard, workers=settings.browser_workers, engine=settings.browser_engine, warm=settings.browser_warm
            )
        )
        if settings.stealth_browser and find_spec("patchright"):
            # Patchright, a Playwright fork patched against automation detection, for sites that still block
            # the browser. It starts on first use, so a second Chromium takes memory only once a site needs it.
            tiers.append(BrowserFetcher(guard, name="stealth", engine="patchright", workers=1, warm=False))
        elif settings.stealth_browser:
            logger.warning("STEALTH_BROWSER is on but patchright is not installed; the stealth tier is off.")
    return tiers
