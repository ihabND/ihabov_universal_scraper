import logging
from typing import Literal
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from auth import is_valid_key, require_api_key
from functions import (
    ScrapeError,
    InvalidURLError,
    normalize_url,
    fetch_page,
    clean_html_response,
)

class ScrapeResponse(BaseModel):
    url: str
    status_code: int
    ok: bool
    content: str | list[str]

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

def scrape(url: str, mode: Literal["text", "html"]) -> ScrapeResponse:
    normalized = normalize_url(url)
    if normalized is None:
        raise InvalidURLError("Invalid URL, only HTTP and HTTPS.")
    response = fetch_page(normalized)
    content = clean_html_response(response, mode)
    return ScrapeResponse(
        url=normalized,
        status_code=response.status_code,
        ok=response.ok,
        content=content
    )

@app.get("/scrape", response_model=ScrapeResponse,
         dependencies=[Depends(require_api_key)])
def scrape_url(url: str, mode: Literal["text", "html"] = "text"):
    try:
        return scrape(url, mode)
    except ScrapeError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"mode": "text"})

@app.post("/", response_class=HTMLResponse)
def index_submit(
    request: Request,
    url: str = Form(...),
    mode: Literal["text", "html"] = Form("text"),
    api_key: str = Form(""),
):
    context = {"url": url, "mode": mode, "api_key": api_key}
    status_code = 200
    if not is_valid_key(api_key):
        context["error"] = "Invalid or missing API key."
        status_code = 401
    else:
        try:
            context["result"] = scrape(url, mode)
        except ScrapeError as e:
            context["error"] = str(e)
            status_code = e.status_code
    return templates.TemplateResponse(request, "index.html", context,
                                      status_code=status_code)
