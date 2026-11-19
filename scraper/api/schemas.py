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
    fetched_with: str = "http"
    content: str | None = None
    parsed: dict | list[dict] | None = None
    # Where redirects ended up; `url` is the page asked for. Kept out of the JSON.
    final_url: str | None = Field(default=None, exclude=True)


class BatchRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=BATCH_MAX_URLS)
    mode: Mode = "html"
    browser: BrowserOption = "auto"


class BatchItem(BaseModel):
    url: str  # as it was sent
    status: int
    result: ScrapeResponse | None = None
    detail: str | None = None


class BatchResponse(BaseModel):
    results: list[BatchItem]
    succeeded: int
    failed: int
