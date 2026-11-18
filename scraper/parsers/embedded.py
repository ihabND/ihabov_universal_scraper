"""JSON mode on pages without a site parser: the structured data the page carries for its own scripts.

Many pages that look empty without JavaScript (Target, Zillow, YouTube, Tumblr in the benchmark) ship
their data as JSON in the HTML, so it can be read without a browser.
"""

import json

from bs4 import BeautifulSoup

from ..core.errors import ParseError
from . import jsonld

# Data assigned to a variable in an inline script, like `var ytInitialData = {...};`.
ASSIGNED = {
    "ytInitialData": "yt_initial_data",
    "__INITIAL_STATE__": "initial_state",
    "__PRELOADED_STATE__": "preloaded_state",
    "__APOLLO_STATE__": "apollo_state",
}
DECODER = json.JSONDecoder()


def embedded_data(soup: BeautifulSoup, url: str) -> dict:
    data = {}
    items = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            items.extend(jsonld.items(json.loads(tag.string or "")))
        except ValueError:
            continue
    if items:
        data["json_ld"] = items
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data and next_data.string:
        try:
            data["next_data"] = json.loads(next_data.string)
        except ValueError:
            pass
    for tag in soup.find_all("script"):
        text = tag.string or ""
        for variable, key in ASSIGNED.items():
            if key not in data and (found := assigned_json(text, variable)) is not None:
                data[key] = found
    if not data:
        raise ParseError("Couldn't find structured data on this page.")
    title = soup.title.get_text(strip=True) if soup.title else None
    return {"title": title, **data}


def assigned_json(script: str, variable: str):
    """The JSON object assigned to `variable` in a script, or None (e.g. when it's built by a function call)."""
    at = script.find(variable)
    if at < 0:
        return None
    equals = script.find("=", at + len(variable))
    start = script.find("{", equals)
    if equals < 0 or start < 0 or script[equals + 1 : start].strip():
        return None
    try:
        return DECODER.raw_decode(script, start)[0]
    except ValueError:
        return None
