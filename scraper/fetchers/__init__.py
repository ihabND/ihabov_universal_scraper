from ..net.guard import EgressGuard
from ..settings import settings
from .base import Fetcher
from .browser import BrowserFetcher
from .http import HttpFetcher


def build_tiers(guard: EgressGuard) -> list[Fetcher]:
    """The fetchers, cheapest first. A new tier (a stealth browser, an unblocker API) is added here."""
    tiers: list[Fetcher] = [
        HttpFetcher(guard),
        # Sites that block curl's Chrome fingerprint often let its Safari one through (7 of 100 in the
        # benchmark). No retries: the Chrome tier already retried temporary errors.
        HttpFetcher(guard, name="http-safari", impersonate="safari", retries=0),
    ]
    if settings.browser_enabled:
        tiers.append(
            BrowserFetcher(
                guard, workers=settings.browser_workers, engine=settings.browser_engine, warm=settings.browser_warm
            )
        )
    return tiers
