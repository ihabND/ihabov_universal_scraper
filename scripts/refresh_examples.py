"""Refresh scraper/examples.json, the JSON examples shown under "Supported sites" on the front page.

Scrapes each supported site's example URL with the real scraper, so the examples are real output,
then shortens them to fit on the page. Run it from the project root after adding or changing a parser:

    python scripts/refresh_examples.py

It contacts the real sites, and IMDb needs the headless browser (`playwright install chromium`).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper.app import SITE_CARDS  # noqa: E402
from scraper.service import scrape  # noqa: E402

OUTPUT = ROOT / "scraper" / "examples.json"
LIST_ITEMS = 2  # items kept from a page that is a list, like a book listing
NESTED_ITEMS = 4  # items kept from a list or object inside a result, like a recipe's ingredients
TEXT_LENGTH = 90  # characters kept from long text, like a description


def shorten(value, depth: int = 0):
    if isinstance(value, str) and len(value) > TEXT_LENGTH:
        return value[: TEXT_LENGTH - 1].rstrip() + "…"
    if isinstance(value, list):
        return [shorten(v, depth + 1) for v in (value if depth == 0 else value[:NESTED_ITEMS])]
    if isinstance(value, dict):
        items = list(value.items()) if depth == 0 else list(value.items())[:NESTED_ITEMS]
        return {k: shorten(v, depth + 1) for k, v in items}
    return value


def main() -> None:
    examples = {}
    for card in SITE_CARDS:
        result = scrape(card["example"], "json", "auto")
        parsed = result.parsed
        total = len(parsed) if isinstance(parsed, list) else None
        if isinstance(parsed, list):
            parsed = [shorten(item, 0) for item in parsed[:LIST_ITEMS]]
        else:
            parsed = shorten(parsed)
        examples[card["host"]] = {
            "example": card["example"],
            "url": result.url,
            "fetched_with": result.fetched_with,
            "total": total,
            "parsed": parsed,
        }
        print(f"{card['host']}: {result.fetched_with}, {total or 1} item(s)")
    OUTPUT.write_text(json.dumps(examples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
