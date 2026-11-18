from fastapi import APIRouter, Request, Security

from ..net.proxy import proxy_status
from ..store.cache import redis_status
from .deps import api_key_header, authorize
from .schemas import BatchRequest, BatchResponse, BrowserOption, Mode, ScrapeResponse

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict:
    """Needs no key and never scrapes. Redis is optional, so a Redis problem is reported, not fatal."""
    return {
        "status": "ok",
        "redis": await redis_status(),
        "browser": request.app.state.scraper.browser_status,
        "stealth": request.app.state.scraper.tier_status("stealth"),
        "proxy": proxy_status(),
    }


@router.get("/scrape", response_model=ScrapeResponse)
async def scrape(
    request: Request,
    url: str,
    mode: Mode = "html",
    browser: BrowserOption = "auto",
    api_key: str | None = Security(api_key_header),
):
    owner = await authorize(request, api_key)
    return await request.app.state.scraper.scrape(url, mode, browser, owner=owner)


@router.post("/scrape/batch", response_model=BatchResponse)
async def scrape_batch(request: Request, body: BatchRequest, api_key: str | None = Security(api_key_header)):
    """Up to 50 URLs, each handled as GET /scrape would handle it, with failures reported per URL."""
    owner = await authorize(request, api_key, cost=len(body.urls))
    return await request.app.state.scraper.batch(body.urls, body.mode, body.browser, owner)
