"""What a fetcher is. A new tier, like a stealth browser or an unblocker API, is one more class."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..antibot.clearance import Clearance
from ..core.errors import UnsupportedContentError
from ..core.page import Page
from ..core.strategy import Strategy

MAX_CONTENT_BYTES = 5 * 1024 * 1024
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


@dataclass
class FetchRequest:
    url: str
    owner: str | None = None  # a hash of the API key
    clearance: Clearance | None = None  # this owner's bot-check cookies for the site
    ready: Callable[[bytes], bool] | None = None  # JSON mode: lets a browser stop once the data is there
    after_block: bool = False  # a cheaper fetcher was blocked, so cookies earned now are worth keeping
    queue_timeout: float = 15
    strategy: Strategy = field(default_factory=Strategy)  # the site's settings


class Fetcher(Protocol):
    name: str
    renders: bool  # runs the page's JavaScript
    status: str  # for /health

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def fetch(self, req: FetchRequest) -> Page: ...


def check_content_type(content_type: str) -> None:
    if content_type and not content_type.lower().startswith(HTML_CONTENT_TYPES):
        raise UnsupportedContentError(f"Not an HTML page (Content-Type: {content_type})")
