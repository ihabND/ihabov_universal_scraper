from bs4 import BeautifulSoup

from . import parser
from .text import text_of


@parser(
    "quotes.toscrape.com",
    r"/(page/\d+/?|tag/.+)?",
    "https://quotes.toscrape.com/page/2/",
    label="Quote listing",
    pages="The home page, /page/N/ or /tag/<tag>/",
    shape="list",
    fields="quote, author, tags",
)
def parse_quotes(soup: BeautifulSoup, url: str) -> list[dict]:
    return [
        {
            "quote": (text_of(quote.select_one("span.text")) or "").strip("“”"),
            "author": text_of(quote.select_one("small.author")),
            "tags": [t.get_text(strip=True) for t in quote.select("a.tag")],
        }
        for quote in soup.select("div.quote")
    ]
