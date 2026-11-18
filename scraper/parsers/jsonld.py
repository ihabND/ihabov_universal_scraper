"""schema.org JSON-LD: finding objects, tolerating the shapes sites publish, and generic readers.

A site that publishes Product, Recipe or Movie data needs no parser of its own: its module registers
one of the readers below.
"""

import html
import json
import re

from bs4 import BeautifulSoup

from ..core.errors import ParseError
from .text import digits_only, to_price


def items(data):
    """Every object in a JSON-LD document: one object, a list, or an @graph."""
    if isinstance(data, list):
        for item in data:
            yield from items(item)
    elif isinstance(data, dict):
        yield data
        yield from items(data.get("@graph"))


def find(soup: BeautifulSoup, *types: str) -> dict | None:
    """The first object of the first type found, in the order the types are given."""
    found = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            found.extend(items(json.loads(tag.string or "")))
        except ValueError:
            continue
    for type_name in types:
        for item in found:
            kind = item.get("@type")
            if kind == type_name or (isinstance(kind, list) and type_name in kind):
                return item
    return None


def as_list(value) -> list:
    return [] if value is None else value if isinstance(value, list) else [value]


def first(value):
    return (value[0] if value else None) if isinstance(value, list) else value


def obj(value) -> dict:
    value = first(value)
    return value if isinstance(value, dict) else {}


def text(value) -> str:
    return html.unescape(value) if isinstance(value, str) else ""


def names(value) -> list[str]:
    """Names from one object, a list of objects, or plain strings."""
    found = (item.get("name") if isinstance(item, dict) else item for item in as_list(value))
    return [html.unescape(name) for name in found if isinstance(name, str) and name]


def minutes(duration) -> int | None:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration if isinstance(duration, str) else "")
    if not match:
        return None
    hours, mins = (int(g) if g else 0 for g in match.groups())
    return hours * 60 + mins


def steps(value) -> list[str]:
    """recipeInstructions: plain strings, HowToStep objects, or HowToSection groups of steps."""
    found = []
    for item in as_list(value):
        if isinstance(item, dict) and "itemListElement" in item:
            found.extend(steps(item["itemListElement"]))
            continue
        step = text(item.get("text") if isinstance(item, dict) else item).strip()
        if step:
            found.append(step)
    return found


def product(soup: BeautifulSoup, url: str) -> dict:
    item = find(soup, "Product")
    if item is None:
        raise ParseError("Couldn't find the product on this page.")
    offer = obj(item.get("offers"))
    brand = first(item.get("brand"))
    return {
        "name": item.get("name"),
        "brand": brand.get("name") if isinstance(brand, dict) else brand,
        "price": to_price(str(offer.get("price") or "")),
        "currency": offer.get("priceCurrency"),
        "availability": str(offer.get("availability") or "").rsplit("/", 1)[-1] or None,
        "shipping_price": obj(obj(offer.get("shippingDetails")).get("shippingRate")).get("value"),
        "category": item.get("category"),
        "sku": item.get("sku"),
        "mpn": item.get("mpn"),
        "ean": item.get("gtin13"),
        "description": text(item.get("description")).strip() or None,
        "image": first(item.get("image")),
        "url": offer.get("url") or url,
    }


def recipe(soup: BeautifulSoup, url: str) -> dict:
    item = find(soup, "Recipe")
    if item is None:
        raise ParseError("Couldn't find the recipe on this page.")
    rating = obj(item.get("aggregateRating"))
    recipe_yield = item.get("recipeYield")
    image = first(item.get("image"))
    return {
        "name": text(item.get("name")),
        "description": text(item.get("description")),
        "author": names(item.get("author")),
        "rating": to_price(str(rating.get("ratingValue", ""))),
        "rating_count": digits_only(str(rating.get("ratingCount", ""))),
        "prep_minutes": minutes(item.get("prepTime")),
        "cook_minutes": minutes(item.get("cookTime")),
        "total_minutes": minutes(item.get("totalTime")),
        "yield": (recipe_yield[-1] if recipe_yield else None) if isinstance(recipe_yield, list) else recipe_yield,
        "categories": item.get("recipeCategory") or [],
        "cuisine": item.get("recipeCuisine") or [],
        "ingredients": [text(i) for i in as_list(item.get("recipeIngredient")) if isinstance(i, str)],
        "instructions": steps(item.get("recipeInstructions")),
        "nutrition": {k: v for k, v in obj(item.get("nutrition")).items() if not k.startswith("@")},
        "image": image.get("url") if isinstance(image, dict) else image,
        "date_published": str(item.get("datePublished") or "")[:10] or None,
    }


def movie(soup: BeautifulSoup, url: str) -> dict:
    item = find(soup, "Movie", "TVSeries", "TVMiniSeries", "TVEpisode", "VideoGame")
    if item is None:
        raise ParseError("Couldn't find the title on this page.")
    published = str(item.get("datePublished") or "")
    rating = obj(item.get("aggregateRating"))
    keywords = item.get("keywords")
    keywords = [text(k) for k in keywords] if isinstance(keywords, list) else text(keywords).split(",")
    return {
        "title": text(item.get("name")),
        "alternate_title": text(item.get("alternateName")) or None,
        "type": item.get("@type"),
        "year": int(published[:4]) if published[:4].isdigit() else None,
        "date_published": published or None,
        "content_rating": item.get("contentRating"),
        "genres": as_list(item.get("genre")),
        "rating": rating.get("ratingValue"),
        "rating_count": rating.get("ratingCount"),
        "runtime_minutes": minutes(item.get("duration")),
        "directors": names(item.get("director")),
        "writers": names(item.get("creator")),
        "actors": names(item.get("actor")),
        "keywords": [k.strip() for k in keywords if k.strip()],
        "description": text(item.get("description")) or None,
        "poster": item.get("image"),
        "trailer": obj(item.get("trailer")).get("url"),
        "url": item.get("url") or url,
    }
