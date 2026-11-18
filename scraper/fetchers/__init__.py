from ..net.guard import EgressGuard
from ..settings import settings
from .base import Fetcher
from .browser import BrowserFetcher
from .http import HttpFetcher


def build_tiers(guard: EgressGuard) -> list[Fetcher]:
    """The fetchers, cheapest first. A new tier (a stealth browser, an unblocker API) is added here."""
    tiers: list[Fetcher] = [HttpFetcher(guard)]
    if settings.browser_enabled:
        tiers.append(
            BrowserFetcher(
                guard, workers=settings.browser_workers, engine=settings.browser_engine, warm=settings.browser_warm
            )
        )
    return tiers
