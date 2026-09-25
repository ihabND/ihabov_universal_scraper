import json
import os
import logging
from typing import Literal
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from auth import is_valid_key, require_api_key
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from cache import get_cached, set_cached
from functions import (
    ScrapeError,
    InvalidURLError,
    UnsupportedSiteError,
    normalize_url,
    fetch_page,
    clean_html_response,
)
from parsers import PARSERS, find_parser, parse_page

class ScrapeResponse(BaseModel):
    url: str
    status_code: int
    ok: bool
    content: str | list[str] | None = None
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

def scrape(url: str, mode: Literal["text", "html"], parse: bool = False) -> ScrapeResponse:
    normalized = normalize_url(url)
    if normalized is None:
        raise InvalidURLError("Invalid URL, only HTTP and HTTPS.")

    cache_type = "parsed" if parse else mode
    cached = get_cached(normalized, cache_type)
    if cached is not None:
        logger.info("Cache hit for %s", normalized)
        return ScrapeResponse.model_validate_json(cached)        
    
    parser = None
    if parse:
        parser = find_parser(normalized)
        if parser is None:
            supported = ", ".join(dict.fromkeys(p.host for p in PARSERS))
            raise UnsupportedSiteError(f"Parsing isn't supported for this page. Supported sites: {supported}.")
    response = fetch_page(normalized)
    result = ScrapeResponse(url=normalized, status_code=response.status_code, ok=response.ok)
    if parser:
        result.parsed = parse_page(parser, response)
    else:
        result.content = clean_html_response(response, mode)
    set_cached(normalized, cache_type, result.model_dump_json(), result.ok)
    return result

@app.get("/scrape", response_model=ScrapeResponse,
         dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute")
def scrape_url(request: Request, url: str,
               mode: Literal["text", "html"] = "text", parse: bool = False):
    try:
        return scrape(url, mode, parse)
    except ScrapeError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"mode": "text"})

@app.get("/docs", response_class=HTMLResponse)
def docs(request: Request):
    return templates.TemplateResponse(request, "docs.html", {"parsers": PARSERS})

@app.post("/", response_class=HTMLResponse)
@limiter.limit("10/minute")
def index_submit(
    request: Request,
    url: str = Form(...),
    mode: Literal["text", "html"] = Form("text"),
    parse: bool = Form(False),
    api_key: str = Form(""),
):
    context = {"url": url, "mode": mode, "parse": parse, "api_key": api_key}
    status_code = 200
    if not is_valid_key(api_key):
        context["error"] = "Invalid or missing API key."
        status_code = 401
    else:
        try:
            result = scrape(url, mode, parse)
            context["result"] = result
            if result.parsed is not None:
                context["parsed_json"] = json.dumps(result.parsed, indent=2, ensure_ascii=False)
        except ScrapeError as e:
            context["error"] = str(e)
            status_code = e.status_code
    return templates.TemplateResponse(request, "index.html", context,
                                      status_code=status_code)
