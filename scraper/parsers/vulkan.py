import html
import re

from bs4 import BeautifulSoup

from ..core.errors import ParseError
from . import parser
from .text import eu_number, meta_content, text_of


@parser(
    "knjizare-vulkan.rs",
    r"/[^/]+/\d+-[^/]+/?",
    "https://www.knjizare-vulkan.rs/domaci-klasici/316418-dervis-i-smrt",
    label="Book product page",
    pages="A product page, like /domaci-klasici/316418-dervis-i-smrt",
    fields="name, author, publisher, price, regular_price, currency, available, isbn, sku, product_id, "
    "description, image, url",
)
def parse_vulkan_product(soup: BeautifulSoup, url: str) -> dict:
    title = soup.select_one("h1.nb-product-detail-product-name")
    if title is None:
        raise ParseError("Couldn't find the product on this page.")
    # Only the book's own price block: the page also shows a mini cart and recommended books.
    prices = soup.select_one(".nb-product-price-detail-2")
    sale = prices.select_one(".nb-price-sale .nb-price-value") if prices else None
    regular = prices.select_one(".nb-price-regular .nb-price-value") if prices else None
    currency = re.search(r"\b[A-Z]{3}\b", prices.get_text(" ")) if sale else None
    publisher_label = soup.select_one(".nb-product-publisher")
    isbn = re.search(r"[\dX]{10,13}", text_of(soup.select_one(".nb-product-isbn")) or "")
    product_id = re.search(r"/(\d+)-[^/]+/?$", url)
    return {
        "name": title.get_text(" ", strip=True),
        "author": text_of(soup.select_one(".nb-product-author-component a")),
        "publisher": text_of(publisher_label.find_next_sibling("a")) if publisher_label else None,
        "price": eu_number(sale.get_text()) if sale else None,
        "regular_price": eu_number(regular.get_text()) if regular else None,
        "currency": currency.group() if currency else None,
        "available": sale is not None,
        "isbn": isbn.group() if isbn else None,
        "sku": text_of(soup.select_one(".nb-product-code-id")),
        "product_id": product_id.group(1) if product_id else None,
        "description": html.unescape(meta_content(soup, "description") or "").strip() or None,
        "image": meta_content(soup, "image"),
        "url": url,
    }
