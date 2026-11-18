from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..core.errors import ParseError
from . import parser
from .text import digits_only, text_of

ROW_KEYS = {
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
NUMBER_KEYS = {"year", "power_hp", "mileage_km"}


@parser(
    "webscraper.io",
    r"/test-sites/product/[^/]+/?",
    "https://webscraper.io/test-sites/product/bmw-e24-635csi-1954-c002",
    label="Car page",
    pages="A car page under /test-sites/product/",
    fields="brand, model, year, engine, drivetrain, power_hp, country, mileage_km, condition, exterior_color, "
    "interior_color, body, price_eur, availability, rating, seller, transmission, tags, colors, image",
)
def parse_car(soup: BeautifulSoup, url: str) -> dict:
    car = {}
    for row in soup.select("div.vehicle-details tr"):
        key, value = ROW_KEYS.get(text_of(row.find("th"))), text_of(row.find("td"))
        if key and value is not None:
            car[key] = digits_only(value) if key in NUMBER_KEYS else value
    if not car:
        raise ParseError("Couldn't find the car details on this page.")
    price, rating = soup.select_one("span.amount"), soup.select_one("div.rarity-rating")
    image = soup.select_one("#main-product-image")
    car["price_eur"] = digits_only(price.get_text()) if price else None
    car["availability"] = text_of(soup.select_one("div.availability"))
    car["rating"] = digits_only(rating.get("data-rating", "")) if rating else None
    car["seller"] = text_of(soup.select_one("p.seller"))
    car["transmission"] = [o.get("value") for o in soup.select("select.transmission option")]
    car["tags"] = [t.get_text(strip=True) for t in soup.select("div.tags span.tag")]
    car["colors"] = [b.get("value") for b in soup.select("#color-swatches button")]
    car["image"] = urljoin(url, image["src"]) if image and image.get("src") else None
    return car
