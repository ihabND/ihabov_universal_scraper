"""The FastAPI app: the API, the web pages, and the scraper's lifetime. Run with `uvicorn scraper.app:app`."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router as api_router
from .core.errors import ScrapeError
from .core.service import Scraper
from .fetchers import build_tiers
from .net.guard import EgressGuard
from .store import cache
from .web.views import STATIC_DIR
from .web.views import router as web_router

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    guard = EgressGuard()
    app.state.scraper = Scraper(build_tiers(guard), guard)
    await app.state.scraper.start()
    try:
        yield
    finally:
        await app.state.scraper.stop()
        await cache.close()


app = FastAPI(title="Ihabov Universal Scraper", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(api_router)
app.include_router(web_router)


@app.exception_handler(ScrapeError)
async def scrape_error(request: Request, exc: ScrapeError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response
