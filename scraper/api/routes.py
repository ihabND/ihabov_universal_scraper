from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Security

from ..net.proxy import proxy_status
from ..net.urls import host_in
from ..settings import settings
from ..store.cache import redis_status
from .deps import api_key_header, authorize, is_test_key
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
    owner = await authorize(request, api_key, allow_test=True)
    test = is_test_key(api_key)
    if test:
        check_test_site(url)
    result = await request.app.state.scraper.scrape(url, mode, browser, owner=owner)
    if test:
        check_test_site(result.final_url or result.url)  # a redirect must not make the public key a proxy
    return result


def check_test_site(url: str) -> None:
    host = urlsplit(url.strip()).hostname or ""
    if not host_in(host, settings.test_key_sites):
        sites = ", ".join(settings.test_key_sites)
        raise HTTPException(403, f"The test key works on the example sites only: {sites}.")


@router.post("/scrape/batch", response_model=BatchResponse)
async def scrape_batch(request: Request, body: BatchRequest, api_key: str | None = Security(api_key_header)):
    """Up to 50 URLs, each handled as GET /scrape would handle it, with failures reported per URL."""
    owner = await authorize(request, api_key, cost=len(body.urls))
    return await request.app.state.scraper.batch(body.urls, body.mode, body.browser, owner)
