"""Small helpers for reading values out of HTML."""

import re

from bs4 import BeautifulSoup


def text_of(tag) -> str | None:
    return tag.get_text(strip=True) if tag else None


def digits_only(text: str) -> int | None:
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def to_price(text: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    return float(match.group()) if match else None


def eu_number(text: str) -> float | None:
    """A number written the Serbian/European way: dots group thousands, a comma marks decimals."""
    match = re.search(r"\d[\d.]*(?:,\d+)?", text)
    return float(match.group().replace(".", "").replace(",", ".")) if match else None


def meta_content(soup: BeautifulSoup, itemprop: str) -> str | None:
    tag = soup.find("meta", attrs={"itemprop": itemprop})
    return (tag.get("content") or "").strip() or None if tag else None
