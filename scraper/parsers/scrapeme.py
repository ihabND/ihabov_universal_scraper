from bs4 import BeautifulSoup

from ..core.errors import ParseError
from . import parser
from .text import digits_only, text_of, to_price


@parser(
    "scrapeme.live",
    r"/shop/(?!page/)[^/]+/?",
    "https://scrapeme.live/shop/Bulbasaur/",
    label="Product page",
    pages="A product page, like /shop/Bulbasaur/",
    fields="name, price, description, sku, categories, tags, stock",
)
def parse_pokemon(soup: BeautifulSoup, url: str) -> dict:
    summary = soup.find("div", class_="summary")
    if summary is None:
        raise ParseError("Couldn't find the product on this page.")
    return {
        "name": text_of(summary.select_one("h1.product_title")),
        "price": to_price(text_of(summary.select_one("span.woocommerce-Price-amount")) or ""),
        "description": text_of(summary.select_one("div.woocommerce-product-details__short-description")),
        "sku": text_of(summary.select_one("span.sku")),
        "categories": [a.get_text(strip=True) for a in summary.select("span.posted_in a")],
        "tags": [a.get_text(strip=True) for a in summary.select("span.tagged_as a")],
        "stock": digits_only(text_of(summary.select_one("p.stock")) or ""),
    }
