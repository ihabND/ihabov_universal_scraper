"""A fetched page, as the rest of the scraper sees it."""

from dataclasses import dataclass
from typing import Literal


@dataclass
class Page:
    url: str
    status_code: int
    content: bytes
    fetched_with: Literal["http", "browser"] = "http"
    # False when the browser stopped early because the JSON data was already there. Such a page has
    # what the parser needs but may lack content that loads later, so HTML mode never reuses it.
    complete: bool = True

    @property
    def ok(self) -> bool:
        return self.status_code < 400
