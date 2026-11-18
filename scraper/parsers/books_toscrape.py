from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..core.errors import ParseError
from . import parser
from .text import digits_only, text_of, to_price

RATINGS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


def rating_of(tag) -> int | None:
    return next((RATINGS[c] for c in (tag.get("class", []) if tag else []) if c in RATINGS), None)


@parser(
    "books.toscrape.com",
    r"/(index\.html)?|/catalogue/(page-\d+\.html|category/.+)",
    "https://books.toscrape.com/catalogue/page-2.html",
    label="Book listing",
    pages="The home page, /catalogue/page-N.html or a category page",
    shape="list",
    fields="title, price, stock, rating (1–5), url",
)
def parse_books(soup: BeautifulSoup, url: str) -> list[dict]:
    return [
        {
            "title": link.get("title"),
            "price": to_price(text_of(card.select_one("p.price_color")) or ""),
            "stock": text_of(card.select_one("p.instock.availability")),
            "rating": rating_of(card.select_one("p.star-rating")),
            "url": urljoin(url, link.get("href", "")),
        }
        for card in soup.select("article.product_pod")
        if (link := card.select_one("h3 a"))
    ]


@parser(
    "books.toscrape.com",
    r"/catalogue/(?!category/)[^/]+_\d+/index\.html",
    "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
    label="Book page",
    pages="A book page, like /catalogue/a-light-in-the-attic_1000/index.html",
    fields="title, genre, price, price_incl_tax, tax, stock, rating (1–5), reviews, upc, description, image",
)
def parse_book(soup: BeautifulSoup, url: str) -> dict:
    product = soup.find("div", class_="product_main")
    if product is None:
        raise ParseError("Couldn't find the book on this page.")
    info = {text_of(row.th): text_of(row.td) for row in soup.select("table.table-striped tr")}
    breadcrumb = soup.select("ul.breadcrumb li a")
    description = text_of(soup.select_one("#product_description + p"))
    image = soup.select_one("#product_gallery img")
    return {
        "title": text_of(product.h1),
        "genre": text_of(breadcrumb[-1]) if len(breadcrumb) > 2 else None,
        "price": to_price(info.get("Price (excl. tax)") or ""),
        "price_incl_tax": to_price(info.get("Price (incl. tax)") or ""),
        "tax": to_price(info.get("Tax") or ""),
        "stock": digits_only(info.get("Availability") or ""),
        "rating": rating_of(product.select_one("p.star-rating")),
        "reviews": digits_only(info.get("Number of reviews") or ""),
        "upc": info.get("UPC"),
        "description": description.removesuffix("...more").strip() if description else None,
        "image": urljoin(url, image["src"]) if image and image.get("src") else None,
    }
