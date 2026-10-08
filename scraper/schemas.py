"""Request and response bodies of the API."""

from typing import Literal

from pydantic import BaseModel, Field

Mode = Literal["html", "json"]
BrowserOption = Literal["auto", "never", "always"]

BATCH_MAX_URLS = 50


class ScrapeResponse(BaseModel):
    url: str
    status_code: int
    ok: bool
    fetched_with: Literal["http", "browser"] = "http"
    content: str | None = None
    parsed: dict | list[dict] | None = None


class BatchRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=BATCH_MAX_URLS)
    mode: Mode = "html"
    browser: BrowserOption = "auto"


class BatchItem(BaseModel):
    url: str  # the URL as it was sent
    status: int  # 200 when scraped, otherwise the status /scrape would have returned
    result: ScrapeResponse | None = None
    detail: str | None = None  # the error message when status isn't 200


class BatchResponse(BaseModel):
    results: list[BatchItem]  # in the same order as the URLs that were sent
    succeeded: int
    failed: int
