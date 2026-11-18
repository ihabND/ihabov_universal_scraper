from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

import lxml.html
from lxml import etree

from ..antibot.detect import looks_blocked

if TYPE_CHECKING:
    from ..antibot.clearance import Clearance

# A page this big with this little visible text was built by JavaScript (or is a JavaScript check).
EMPTY_PAGE_MIN_BYTES = 20 * 1024
EMPTY_PAGE_MAX_TEXT = 400


@dataclass
class Page:
    url: str
    status_code: int
    content: bytes
    fetched_with: str = "http"
    # False when a browser stopped as soon as the JSON data was there; HTML mode never reuses such a page.
    complete: bool = True
    earned: "Clearance | None" = None  # bot-check cookies a browser earned on this page

    @cached_property
    def blocked(self) -> bool:
        return looks_blocked(self.status_code, self.content)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300 and not self.blocked


def looks_empty(page: Page) -> bool:
    if len(page.content) < EMPTY_PAGE_MIN_BYTES:
        return False
    try:
        tree = lxml.html.document_fromstring(page.content)
    except etree.ParserError:
        return False
    etree.strip_elements(tree, "script", "style", "noscript", "template", with_tail=False)
    return len(" ".join(tree.text_content().split())) < EMPTY_PAGE_MAX_TEXT
