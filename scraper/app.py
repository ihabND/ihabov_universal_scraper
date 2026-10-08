"""The web app: the JSON API, the web pages, and the health check."""

import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cache
from .auth import api_key_header, is_valid_key
from .browser import BROWSER_ENABLED
from .errors import ScrapeError
from .parsers import PARSERS
from .quota import key_id, take_ip, take_key
from .schemas import BatchRequest, BatchResponse, BrowserOption, Mode, ScrapeResponse
from .service import run_batch, scrape

PACKAGE_DIR = Path(__file__).parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Ihabov Universal Scraper",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")


class RateLimited(Exception):
    def __init__(self, limit, reset_at: float):
        self.limit, self.reset_at = limit, reset_at


@app.exception_handler(RateLimited)
def rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
    # Same body as before (slowapi's format), plus a Retry-After header.
    retry_after = max(1, int(exc.reset_at - time.time()) + 1)
    return JSONResponse(
        {"error": f"Rate limit exceeded: {exc.limit}"}, status_code=429, headers={"Retry-After": str(retry_after)}
    )


def client_ip(request: Request) -> str:
    return request.headers.get("fly-client-ip") or (request.client.host if request.client else "unknown")


def authorize(request: Request, api_key: str | None, cost: int = 1) -> str:
    """Check the key and charge the right limit. Returns the owner id for the key.
    A missing or wrong key is counted against the caller's IP (10 a minute by default), so guessing
    keys stays slow. A valid key is charged per URL against its own quota."""
    if not is_valid_key(api_key):
        exceeded = take_ip(client_ip(request))
        if exceeded:
            raise RateLimited(*exceeded)
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    exceeded = take_key(api_key, cost)
    if exceeded:
        raise RateLimited(*exceeded)
    return key_id(api_key)


@app.get("/health")
def health() -> dict:
    """For uptime monitors and the hosting platform. Needs no key and never scrapes anything.
    The app works without Redis (just without the cache), so a Redis problem is reported, not fatal."""
    return {"status": "ok", "redis": cache.redis_status(), "browser": "on" if BROWSER_ENABLED else "off"}


# ---- API -------------------------------------------------------------------------------------


@app.get("/scrape", response_model=ScrapeResponse)
def scrape_url(
    request: Request,
    url: str,
    mode: Mode = "html",
    browser: BrowserOption = "auto",
    api_key: str | None = Security(api_key_header),
):
    # The key is checked inside the endpoint, before anything is fetched, so that wrong keys count
    # toward the IP limit too.
    owner = authorize(request, api_key)
    try:
        return scrape(url, mode, browser, owner=owner)
    except ScrapeError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e


@app.post("/scrape/batch", response_model=BatchResponse)
def scrape_batch(request: Request, body: BatchRequest, api_key: str | None = Security(api_key_header)):
    """Scrape up to 50 URLs in one call. Every URL is handled exactly as GET /scrape would handle
    it; failures are reported per URL instead of failing the whole batch."""
    owner = authorize(request, api_key, cost=len(body.urls))
    return run_batch(body.urls, body.mode, body.browser, owner)


# ---- Web pages -----------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, url: str = "", mode: Mode = "html", browser: BrowserOption = "auto"):
    # url/mode/browser in the query string only prefill the form, nothing is fetched on GET.
    return templates.TemplateResponse(
        request, "index.html", {"url": url, "mode": mode, "browser": browser, "parsers": PARSERS}
    )


@app.get("/docs", response_class=HTMLResponse)
def docs(request: Request):
    return templates.TemplateResponse(request, "docs.html", {"parsers": PARSERS})


@app.post("/", response_class=HTMLResponse)
def index_submit(
    request: Request,
    url: str = Form(...),
    mode: Mode = Form("html"),
    browser: BrowserOption = Form("auto"),
    api_key: str = Form(""),
):
    # Fallback for browsers without JavaScript; the page itself normally calls /scrape directly.
    context = {"url": url, "mode": mode, "browser": browser, "api_key": api_key, "parsers": PARSERS}
    status_code = 200
    try:
        owner = authorize(request, api_key)
    except HTTPException:
        owner = None
        context["error"] = "Invalid or missing API key."
        status_code = 401
    except RateLimited as e:
        owner = None
        context["error"] = f"Rate limit exceeded: {e.limit}. Wait a moment and try again."
        status_code = 429
    if owner is not None:
        try:
            result = scrape(url, mode, browser, owner=owner)
            context["result"] = result
            # Embedded in a <script type="application/json"> tag, so "<" is escaped: a scraped page
            # containing "</script>" must not be able to break out of that tag.
            context["seed"] = result.model_dump_json().replace("<", "\\u003c")
            if result.parsed is not None:
                context["parsed_json"] = json.dumps(result.parsed, indent=2, ensure_ascii=False)
        except ScrapeError as e:
            context["error"] = str(e)
            status_code = e.status_code
    return templates.TemplateResponse(request, "index.html", context, status_code=status_code)
