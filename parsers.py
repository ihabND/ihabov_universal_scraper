import re
import json
import html
import logging
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from functions import Page, ParseError

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

def as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]

def names_of(value) -> list[str]:
    """Names from a JSON-LD value that may be one object, a list of objects, or plain strings."""
    names = []
    for item in as_list(value):
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name:
            names.append(html.unescape(name))
    return names

def json_ld_items(data):
    """Every object in a JSON-LD document, whatever its shape: one object, a list, or an @graph."""
    if isinstance(data, list):
        for item in data:
            yield from json_ld_items(item)
    elif isinstance(data, dict):
        yield data
        yield from json_ld_items(data.get("@graph"))

def find_json_ld(soup: BeautifulSoup, type_name: str) -> dict | None:
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        for item in json_ld_items(data):
            types = item.get("@type")
            if type_name == types or (isinstance(types, list) and type_name in types):
                return item
    return None

def to_minutes(duration: str | None) -> int | None:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration or "")
    if not match:
        return None
    hours, minutes = (int(g) if g else 0 for g in match.groups())
    return hours * 60 + minutes

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
    price = soup.select_one("span.amount")
    car["price_eur"] = digits_only(price.get_text()) if price else None
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

def parse_vulkan_product(soup: BeautifulSoup, url: str) -> dict:
    product = find_json_ld(soup, "Product")
    if product is None:
        raise ParseError("Couldn't find the product on this page.")
    offer = product.get("offers") or {}
    return {
        "name": product.get("name"),
        "publisher": (product.get("brand") or {}).get("name"),
        "price": to_price(str(offer.get("price", ""))),
        "currency": offer.get("priceCurrency"),
        "isbn": product.get("isbn"),
        "sku": product.get("sku"),
        "product_id": product.get("productID"),
        "description": html.unescape(product.get("description") or "").strip() or None,
        "image": product.get("image"),
        "url": offer.get("url") or url,
    }

def parse_ipon_product(soup: BeautifulSoup, url: str) -> dict:
    product = find_json_ld(soup, "Product")
    if product is None:
        raise ParseError("Couldn't find the product on this page.")
    offer = product.get("offers") or {}
    shipping = (offer.get("shippingDetails") or {}).get("shippingRate") or {}
    images = product.get("image") or []
    return {
        "name": product.get("name"),
        "brand": (product.get("brand") or {}).get("name"),
        "price": to_price(str(offer.get("price") or "")),
        "currency": offer.get("priceCurrency"),
        "availability": (offer.get("availability") or "").rsplit("/", 1)[-1] or None,
        "shipping_price": shipping.get("value"),
        "category": product.get("category"),
        "sku": product.get("sku"),
        "mpn": product.get("mpn"),
        "ean": product.get("gtin13"),
        "description": (product.get("description") or "").strip() or None,
        "image": images[0] if isinstance(images, list) and images else images or None,
        "url": offer.get("url") or url,
    }

def instruction_steps(value) -> list[str]:
    """Flatten recipeInstructions: plain strings, HowToStep objects, or HowToSection groups of steps."""
    steps = []
    for item in as_list(value):
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict) and "itemListElement" in item:
            steps.extend(instruction_steps(item["itemListElement"]))
            continue
        elif isinstance(item, dict):
            text = item.get("text") or ""
        else:
            continue
        text = html.unescape(text).strip()
        if text:
            steps.append(text)
    return steps

def parse_recipe(soup: BeautifulSoup, url: str) -> dict:
    recipe = find_json_ld(soup, "Recipe")
    if recipe is None:
        raise ParseError("Couldn't find the recipe on this page.")
    rating = recipe.get("aggregateRating") or {}
    recipe_yield = recipe.get("recipeYield")
    if isinstance(recipe_yield, list):
        recipe_yield = recipe_yield[-1] if recipe_yield else None
    image = recipe.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")
    return {
        "name": html.unescape(recipe.get("name") or ""),
        "description": html.unescape(recipe.get("description") or ""),
        "author": names_of(recipe.get("author")),
        "rating": to_price(str(rating.get("ratingValue", ""))),
        "rating_count": digits_only(str(rating.get("ratingCount", ""))),
        "prep_minutes": to_minutes(recipe.get("prepTime")),
        "cook_minutes": to_minutes(recipe.get("cookTime")),
        "total_minutes": to_minutes(recipe.get("totalTime")),
        "yield": recipe_yield,
        "categories": recipe.get("recipeCategory") or [],
        "cuisine": recipe.get("recipeCuisine") or [],
        "ingredients": [html.unescape(i) for i in recipe.get("recipeIngredient") or []],
        "instructions": instruction_steps(recipe.get("recipeInstructions")),
        "nutrition": {k: v for k, v in (recipe.get("nutrition") or {}).items() if not k.startswith("@")},
        "image": image,
        "date_published": (recipe.get("datePublished") or "")[:10] or None,
    }

IMDB_TYPES = ("Movie", "TVSeries", "TVMiniSeries", "TVEpisode", "VideoGame")

def parse_movie(soup: BeautifulSoup, url: str) -> dict:
    movie = next((m for t in IMDB_TYPES if (m := find_json_ld(soup, t))), None)
    if movie is None:
        raise ParseError("Couldn't find the title on this page.")
    date_published = movie.get("datePublished") or ""
    rating = movie.get("aggregateRating") or {}
    keywords = html.unescape(movie.get("keywords") or "")
    return {
        "title": html.unescape(movie.get("name") or ""),
        "alternate_title": html.unescape(movie.get("alternateName") or "") or None,
        "type": movie.get("@type"),
        "year": int(date_published[:4]) if date_published[:4].isdigit() else None,
        "date_published": date_published or None,
        "content_rating": movie.get("contentRating"),
        "genres": as_list(movie.get("genre")),
        "rating": rating.get("ratingValue"),
        "rating_count": rating.get("ratingCount"),
        "runtime_minutes": to_minutes(movie.get("duration")),
        "directors": names_of(movie.get("director")),
        "writers": names_of(movie.get("creator")),
        "actors": names_of(movie.get("actor")),
        "keywords": [k.strip() for k in keywords.split(",") if k.strip()],
        "description": html.unescape(movie.get("description") or "") or None,
        "poster": movie.get("image"),
        "trailer": (movie.get("trailer") or {}).get("url"),
        "url": movie.get("url") or url,
    }

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
    Parser("knjizare-vulkan.rs", re.compile(r"/[^/]+/\d+-[^/]+/?"),
           "https://www.knjizare-vulkan.rs/domaci-klasici/25486-tvrdava", parse_vulkan_product),
    Parser("iponcomp.com", re.compile(r"/shop/product/[^/]+/\d+/?"),
           "https://iponcomp.com/shop/product/valueline-usb-32-gen-1-hub-4-ports-type-c-connection-cable/1871163", parse_ipon_product),
    Parser("allrecipes.com", re.compile(r"/recipe/\d+/[^/]+/?|/[^/]+-recipe-\d+/?"),
           "https://www.allrecipes.com/recipe/10813/best-chocolate-chip-cookies/", parse_recipe),
    Parser("imdb.com", re.compile(r"/title/tt\d+/?"),
           "https://www.imdb.com/title/tt0111161/", parse_movie),
]

def find_parser(url: str) -> Parser | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    for parser in PARSERS:
        if host == parser.host and parser.path.fullmatch(parts.path):
            return parser
    return None

def parse_page(parser: Parser, response: Page) -> Parsed:
    soup = BeautifulSoup(response.content, "lxml")
    try:
        result = parser.parse(soup, response.url)
    except (AttributeError, KeyError, TypeError, IndexError) as e:
        logger.warning("Parser for %s failed on %s: %r", parser.host, response.url, e)
        raise ParseError("Couldn't find the expected data on this page.") from e
    if not result:
        raise ParseError("Couldn't find the expected data on this page.")
    return result
