from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

from ..antibot.detect import looks_blocked

if TYPE_CHECKING:
    from ..antibot.clearance import Clearance


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
