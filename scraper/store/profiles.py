"""What the scraper has learned about each site, kept for a day.

When a profile expires, the next request starts from the cheapest fetcher again, which is how a
site that stopped blocking (or a page that stopped needing JavaScript) gets noticed.
"""

from dataclasses import asdict, dataclass, field

from .cache import get_json, set_json

PROFILE_SECONDS = 24 * 3600


@dataclass
class SiteProfile:
    tier: str = "http"  # the cheapest fetcher that got through
    render: list[str] = field(default_factory=list)  # parsers whose data needs a fetcher that runs JavaScript


async def get_profile(site: str) -> SiteProfile | None:
    record = await get_json(f"site:{site}")
    try:
        return SiteProfile(**record) if record else None
    except TypeError:
        return None


async def save_profile(site: str, profile: SiteProfile) -> None:
    await set_json(f"site:{site}", asdict(profile), PROFILE_SECONDS)
