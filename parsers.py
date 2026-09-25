import re
import logging
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from curl_cffi import requests
from functions import ParseError

logger = logging.getLogger(__name__)

Parsed = dict | list[dict]

@dataclass(frozen=True)
class Parser:
    host: str
    path: re.Pattern
    example: str
    parse: Callable[[BeautifulSoup, str], Parsed]

def digits_only(text: str) -> int | None:
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None

def to_price(text: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    return float(match.group()) if match else None

def text_of(tag) -> str | None:
    return tag.get_text(strip=True) if tag else None

CAR_FIELDS = {
    "Make": "brand",
    "Model": "model",
    "Year": "year",
    "Engine": "engine",
    "Drivetrain": "drivetrain",
    "Power": "power_hp",
    "Country of Origin": "country",
    "Mileage": "mileage_km",
    "Condition": "condition",
    "Exterior Color": "exterior_color",
    "Interior Color": "interior_color",
    "Body style": "body",
}
CAR_NUMBER_FIELDS = {"year", "power_hp", "mileage_km"}

def parse_car(soup: BeautifulSoup, url: str) -> dict:
    car = {}
    for row in soup.select("div.vehicle-details tr"):
        label, value = row.find("th"), row.find("td")
        key = CAR_FIELDS.get(text_of(label))
        if key is None or value is None:
            continue
        value = value.get_text(strip=True)
        car[key] = digits_only(value) if key in CAR_NUMBER_FIELDS else value
    if not car:
        raise ParseError("Couldn't find the car details on this page.")

    rating = soup.select_one("div.rarity-rating")
    image = soup.select_one("#main-product-image")
    car["price_eur"] = digits_only(soup.select_one("span.amount").get_text())
    car["availability"] = text_of(soup.select_one("div.availability"))
    car["rating"] = digits_only(rating.get("data-rating", "")) if rating else None
    car["seller"] = text_of(soup.select_one("p.seller"))
    car["transmission"] = [o["value"] for o in soup.select("select.transmission option")]
    car["tags"] = [t.get_text(strip=True) for t in soup.select("div.tags span.tag")]
    car["colors"] = [b["value"] for b in soup.select("#color-swatches button")]
    car["image"] = urljoin(url, image["src"]) if image else None
    return car

def parse_pokemon(soup: BeautifulSoup, url: str) -> dict:
    summary = soup.find("div", class_="summary")
    if summary is None:
        raise ParseError("Couldn't find the product on this page.")
    stock = text_of(summary.select_one("p.stock")) or ""
    return {
        "name": text_of(summary.select_one("h1.product_title")),
        "price": to_price(summary.select_one("span.woocommerce-Price-amount").get_text()),
        "description": text_of(summary.select_one("div.woocommerce-product-details__short-description")),
        "sku": text_of(summary.select_one("span.sku")),
        "categories": [a.get_text(strip=True) for a in summary.select("span.posted_in a")],
        "tags": [a.get_text(strip=True) for a in summary.select("span.tagged_as a")],
        "stock": digits_only(stock),
    }

BOOK_RATINGS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}

def parse_books(soup: BeautifulSoup, url: str) -> list[dict]:
    books = []
    for card in soup.select("article.product_pod"):
        rating_classes = card.select_one("p.star-rating")["class"]
        books.append({
            "title": card.h3.a["title"],
            "price": to_price(card.select_one("p.price_color").get_text()),
            "stock": text_of(card.select_one("p.instock.availability")),
            "rating": next((BOOK_RATINGS[c] for c in rating_classes if c in BOOK_RATINGS), None),
            "url": urljoin(url, card.h3.a["href"]),
        })
    return books

def parse_book(soup: BeautifulSoup, url: str) -> dict:
    product = soup.find("div", class_="product_main")
    if product is None:
        raise ParseError("Couldn't find the book on this page.")
    info = {text_of(row.th): text_of(row.td) for row in soup.select("table.table-striped tr")}
    breadcrumb = soup.select("ul.breadcrumb li a")
    rating_classes = product.select_one("p.star-rating")["class"]
    description = soup.select_one("#product_description + p")
    image = soup.select_one("#product_gallery img")
    return {
        "title": text_of(product.h1),
        "genre": text_of(breadcrumb[-1]) if len(breadcrumb) > 2 else None,
        "price": to_price(info.get("Price (excl. tax)", "")),
        "price_incl_tax": to_price(info.get("Price (incl. tax)", "")),
        "tax": to_price(info.get("Tax", "")),
        "stock": digits_only(info.get("Availability", "")),
        "rating": next((BOOK_RATINGS[c] for c in rating_classes if c in BOOK_RATINGS), None),
        "reviews": digits_only(info.get("Number of reviews", "")),
        "upc": info.get("UPC"),
        "description": text_of(description).removesuffix("...more").strip() if description else None,
        "image": urljoin(url, image["src"]) if image else None,
    }

def parse_quotes(soup: BeautifulSoup, url: str) -> list[dict]:
    quotes = []
    for quote in soup.select("div.quote"):
        quotes.append({
            "quote": quote.select_one("span.text").get_text(strip=True).strip("“”"),
            "author": text_of(quote.select_one("small.author")),
            "tags": [t.get_text(strip=True) for t in quote.select("a.tag")],
        })
    return quotes

PARSERS = [
    Parser("webscraper.io", re.compile(r"/test-sites/product/[^/]+/?"),
           "https://webscraper.io/test-sites/product/bmw-e24-635csi-1954-c002", parse_car),
    Parser("scrapeme.live", re.compile(r"/shop/(?!page/)[^/]+/?"),
           "https://scrapeme.live/shop/Bulbasaur/", parse_pokemon),
    Parser("books.toscrape.com", re.compile(r"/(index\.html)?|/catalogue/(page-\d+\.html|category/.+)"),
           "https://books.toscrape.com/catalogue/page-2.html", parse_books),
    Parser("books.toscrape.com", re.compile(r"/catalogue/(?!category/)[^/]+_\d+/index\.html"),
           "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html", parse_book),
    Parser("quotes.toscrape.com", re.compile(r"/(page/\d+/?|tag/.+)?"),
           "https://quotes.toscrape.com/page/2/", parse_quotes),
]

def find_parser(url: str) -> Parser | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    for parser in PARSERS:
        if host == parser.host and parser.path.fullmatch(parts.path):
            return parser
    return None

def parse_page(parser: Parser, response: requests.Response) -> Parsed:
    soup = BeautifulSoup(response.content, "lxml")
    try:
        result = parser.parse(soup, response.url)
    except (AttributeError, KeyError, TypeError, IndexError) as e:
        logger.warning("Parser for %s failed on %s: %r", parser.host, response.url, e)
        raise ParseError("Couldn't find the expected data on this page.") from e
    if not result:
        raise ParseError("Couldn't find the expected data on this page.")
    return result
