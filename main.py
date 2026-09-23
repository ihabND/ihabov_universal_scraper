import logging
from typing import Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from functions import (
    ScrapeError,
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

app = FastAPI(title="Ihabov Universal Scraper")

@app.get("/scrape", response_model=ScrapeResponse)
def scrape_url(url: str, mode: Literal["text", "html"] = "text"):
    normalized = normalize_url(url)
    if normalized is None:
        raise HTTPException(status_code=400,
                            detail="Invalid URL, only HTTP and HTTPS.")
    try:
        response = fetch_page(normalized)
        content = clean_html_response(response, mode)
    except ScrapeError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e
    return ScrapeResponse(
        url=normalized,
        status_code=response.status_code,
        ok=response.ok,
        content=content
    )