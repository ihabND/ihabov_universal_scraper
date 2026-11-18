"""Site parsers for JSON mode: one module per site, each registered with @parser.

A new site is a new module here, imported at the bottom of this file, plus a saved page and its
expected output in tests/fixtures/parsers/.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, SoupStrainer

from ..core.errors import ParseError
from ..core.page import Page

Parsed = dict | list[dict]
JSON_LD_TAGS = SoupStrainer("script", attrs={"type": "application/ld+json"})


@dataclass(frozen=True)
class Parser:
    host: str
    path: re.Pattern
    example: str
    parse: Callable[[BeautifulSoup, str], Parsed]
    label: str  # a few words for the front page card
    pages: str  # which pages it reads, for the docs page
    fields: str  # the keys of `parsed`, for the docs page (a test keeps it in sync)
    shape: str = "object"  # or "list"
    json_ld_only: bool = False  # parse only the JSON-LD tags: about twice as fast on large pages
    min_tier: str = "http"  # "browser" for a site known to block plain requests

    @property
    def name(self) -> str:
        return self.parse.__name__


PARSERS: list[Parser] = []


def parser(host: str, path: str, example: str, **meta) -> Callable:
    def register(parse: Callable) -> Callable:
        PARSERS.append(Parser(host, re.compile(path), example, parse, **meta))
        return parse

    return register


def find_parser(url: str) -> Parser | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    return next((p for p in PARSERS if p.host == host and p.path.fullmatch(parts.path)), None)


def seed_tier(site: str) -> str:
    """Where a site nobody has scraped yet starts: its parsers may know it blocks plain requests."""
    return next((p.min_tier for p in PARSERS if p.host == site and p.min_tier != "http"), "http")


def parse_page(parser: Parser, page: Page) -> Parsed:
    soup = BeautifulSoup(page.content, "lxml", parse_only=JSON_LD_TAGS if parser.json_ld_only else None)
    parsed = parser.parse(soup, page.url)
    if not parsed:
        raise ParseError("Couldn't find the expected data on this page.")
    return parsed


def ready_check(parser: Parser, url: str) -> Callable[[bytes], bool]:
    """True as soon as the parser can read its data, so a browser can stop waiting for the rest of the page."""

    def ready(content: bytes) -> bool:
        try:
            return bool(parse_page(parser, Page(url, 200, content)))
        except ParseError:
            return False

    return ready


# Registration order is the order of the front page cards.
# isort: off
from . import webscraper_io, scrapeme, books_toscrape, quotes_toscrape  # noqa: E402, F401
from . import vulkan, iponcomp, allrecipes, imdb  # noqa: E402, F401
# isort: on
