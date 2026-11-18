"""The home page, the web workbench and the docs page."""

import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..api.deps import TEST_ONLY_SCRAPE, authorize, is_test_key
from ..api.schemas import BrowserOption, Mode
from ..core.errors import ScrapeError
from ..parsers import PARSERS
from ..settings import settings

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
# A fingerprint of each static file in its URL, so browsers never keep an old version.
ASSET_VERSIONS = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()[:10] for path in STATIC_DIR.iterdir()}
SITE_COLORS = ["#3b6fd6", "#2f8a4c", "#b5730b", "#7a4bd0", "#c8372d", "#1b8a8a", "#d0631d", "#a67c00"]

# A real response of the car listing, shown before the first scrape so a cold start is never the first impression.
EXAMPLE = (STATIC_DIR / "example-car.json").read_text(encoding="utf-8").replace("<", "\\u003c")

templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.globals["asset_url"] = lambda name: f"/static/{name}?v={ASSET_VERSIONS[name]}"
router = APIRouter()


def site_cards() -> list[dict]:
    """One card per site; a site with several parsers opens its first example."""
    hosts: dict[str, list] = {}
    for parser in PARSERS:
        hosts.setdefault(parser.host, []).append(parser)
    return [
        {
            "host": host,
            "example": parsers[0].example,
            "label": " or ".join([parsers[0].label, *(p.label.lower() for p in parsers[1:])]),
            "shape": " / ".join(dict.fromkeys(p.shape for p in parsers)),
            "color": SITE_COLORS[i % len(SITE_COLORS)],
        }
        for i, (host, parsers) in enumerate(hosts.items())
    ]


SITE_CARDS = site_cards()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if "url" in request.query_params:  # an old workbench link
        return RedirectResponse(f"/workbench?{request.url.query}", 307)
    context = {"test_key": settings.test_key, "test_key_limits": settings.test_key_limits, "example": EXAMPLE}
    return templates.TemplateResponse(request, "home.html", context)


@router.get("/workbench", response_class=HTMLResponse)
async def index(request: Request, url: str = "", mode: str = "html", browser: str = "auto"):
    # The query string only prefills the form; nothing is fetched on GET.
    context = {"url": url, "mode": mode, "browser": browser, "sites": SITE_CARDS}
    return templates.TemplateResponse(request, "index.html", context)


@router.get("/sites", response_class=HTMLResponse)
async def sites(request: Request):
    """The data comes from /api/sites, read with the key kept in this browser."""
    return templates.TemplateResponse(request, "sites.html")


@router.get("/docs", response_class=HTMLResponse)
async def docs(request: Request):
    return templates.TemplateResponse(request, "docs.html", {"parsers": PARSERS})


@router.post("/workbench", response_class=HTMLResponse)
async def index_submit(
    request: Request,
    url: str = Form(...),
    mode: Mode = Form("html"),
    browser: BrowserOption = Form("auto"),
    api_key: str = Form(""),
):
    """For browsers without JavaScript; the page itself normally calls /scrape."""
    context = {"url": url, "mode": mode, "browser": browser, "api_key": api_key, "sites": SITE_CARDS}
    status_code = 200
    try:
        if is_test_key(api_key):
            raise HTTPException(403, TEST_ONLY_SCRAPE)
        owner = await authorize(request, api_key)
        result = await request.app.state.scraper.scrape(url, mode, browser, owner=owner)
    except HTTPException as e:
        context["error"], status_code = f"{e.detail}.", e.status_code
    except ScrapeError as e:
        context["error"], status_code = str(e), e.status_code
    else:
        context["result"] = result
        # Embedded in a <script type="application/json"> tag: a scraped "</script>" must not end it early.
        context["seed"] = result.model_dump_json().replace("<", "\\u003c")
        if result.parsed is not None:
            context["parsed_json"] = json.dumps(result.parsed, indent=2, ensure_ascii=False)
    return templates.TemplateResponse(request, "index.html", context, status_code=status_code)
