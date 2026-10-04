import json
import os
import logging
from typing import Literal
from fastapi import FastAPI, Form, HTTPException, Request, Security
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from auth import api_key_header, is_valid_key
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from cache import get_cached, set_cached
from functions import (
    BROWSER_ENABLED,
    ScrapeError,
    InvalidURLError,
    ParseError,
    UnsupportedSiteError,
    normalize_url,
    fetch,
    fetch_with_browser,
    clean_html_response,
)
from parsers import PARSERS, find_parser, parse_page

Mode = Literal["html", "json"]
BrowserOption = Literal["auto", "never", "always"]

class ScrapeResponse(BaseModel):
    url: str
    status_code: int
    ok: bool
    fetched_with: Literal["http", "browser"] = "http"
    content: str | None = None
    parsed: dict | list[dict] | None = None

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
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

def client_ip(request: Request) -> str:
    return request.headers.get("fly-client-ip") or get_remote_address(request)

limiter = Limiter(key_func=client_ip,
                  storage_uri=os.environ.get("REDIS_URL", "memory://"),
                  storage_options={"socket_timeout": 1, "socket_connect_timeout": 1},
                  swallow_errors=True,
                  in_memory_fallback_enabled=True,
                )
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

def scrape(url: str, mode: Mode = "html", browser: BrowserOption = "auto") -> ScrapeResponse:
    normalized = normalize_url(url)
    if normalized is None:
        raise InvalidURLError("Invalid URL, only HTTP and HTTPS.")

    cached = get_cached(normalized, mode, browser)
    if cached is not None:
        logger.info("Cache hit for %s", normalized)
        return ScrapeResponse.model_validate_json(cached)

    parser = None
    if mode == "json":
        parser = find_parser(normalized)
        if parser is None:
            supported = ", ".join(dict.fromkeys(p.host for p in PARSERS))
            raise UnsupportedSiteError(f"JSON isn't available for this page. Supported sites: {supported}.")
    page = fetch(normalized, browser)
    if parser:
        try:
            parsed = parse_page(parser, page)
        except ParseError:
            # Only retry with the browser when it could help: an error page (404, 500) will
            # be the same error page in a browser, so don't spend a browser run on it.
            if page.fetched_with == "browser" or browser == "never" or not BROWSER_ENABLED or not page.ok:
                raise
            logger.info("Parsing %s failed, retrying with the browser", page.url)
            page = fetch_with_browser(page.url)
            parsed = parse_page(parser, page)
    result = ScrapeResponse(url=normalized, status_code=page.status_code, ok=page.ok,
                            fetched_with=page.fetched_with)
    if parser:
        result.parsed = parsed
    else:
        result.content = clean_html_response(page)
    set_cached(normalized, mode, browser, result.model_dump_json(), result.ok)
    return result

@app.get("/scrape", response_model=ScrapeResponse)
@limiter.limit("10/minute")
def scrape_url(request: Request, url: str,
               mode: Mode = "html", browser: BrowserOption = "auto",
               api_key: str | None = Security(api_key_header)):
    # The key is checked here, inside the rate-limited function, so that wrong keys count
    # toward the limit too. As a dependency it would run before the limiter and 401s would be free.
    if not is_valid_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    try:
        return scrape(url, mode, browser)
    except ScrapeError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

@app.get("/", response_class=HTMLResponse)
def index(request: Request, url: str = "", mode: Mode = "html", browser: BrowserOption = "auto"):
    # url/mode/browser in the query string only prefill the form, nothing is fetched on GET.
    return templates.TemplateResponse(request, "index.html",
                                      {"url": url, "mode": mode, "browser": browser, "parsers": PARSERS})

@app.get("/docs", response_class=HTMLResponse)
def docs(request: Request):
    return templates.TemplateResponse(request, "docs.html", {"parsers": PARSERS})

@app.post("/", response_class=HTMLResponse)
@limiter.limit("10/minute")
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
    if not is_valid_key(api_key):
        context["error"] = "Invalid or missing API key."
        status_code = 401
    else:
        try:
            result = scrape(url, mode, browser)
            context["result"] = result
            # Embedded in a <script type="application/json"> tag, so "<" is escaped: a scraped page
            # containing "</script>" must not be able to break out of that tag.
            context["seed"] = result.model_dump_json().replace("<", "\\u003c")
            if result.parsed is not None:
                context["parsed_json"] = json.dumps(result.parsed, indent=2, ensure_ascii=False)
        except ScrapeError as e:
            context["error"] = str(e)
            status_code = e.status_code
    return templates.TemplateResponse(request, "index.html", context,
                                      status_code=status_code)
