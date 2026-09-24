"""Call the deployed scraper API from a Python script.

Usage:
    python api_from_script_showcase.py
    python api_from_script_showcase.py https://example.com
    python api_from_script_showcase.py https://example.com --mode html --js

Needs the SCRAPER_API_KEY environment variable (see api_key.md).
"""
import argparse
import os
import sys
import requests

API_URL = "https://ihabov-universal-scraper.fly.dev/scrape"
DEFAULT_URL = "https://www.knjizare-vulkan.rs/domaci-klasici/25486-tvrdava"
# Covers a cold start of the Fly machine plus a slow (browser) scrape.
TIMEOUT_SECONDS = 60

def scrape(session: requests.Session, url: str, mode: str = "text",
           use_js: bool = False) -> dict:
    params = {"url": url, "mode": mode}
    if use_js:
        params["js"] = "true"
    response = session.get(API_URL, params=params, timeout=TIMEOUT_SECONDS)
    if not response.ok:
        # The API explains errors in {"detail": "..."}
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:200]
        raise RuntimeError(f"API returned {response.status_code}: {detail}")
    return response.json()

def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape a page through the API.")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--mode", choices=["text", "html"], default="text")
    parser.add_argument("--js", action="store_true",
                        help="load the page in a real browser (slower)")
    args = parser.parse_args()

    api_key = os.environ.get("SCRAPER_API_KEY")
    if not api_key:
        print("Set the SCRAPER_API_KEY environment variable first.", file=sys.stderr)
        return 1

    # Windows terminals default to a legacy encoding that can't print every character.
    sys.stdout.reconfigure(encoding="utf-8")

    with requests.Session() as session:
        session.headers["X-API-Key"] = api_key
        try:
            data = scrape(session, args.url, args.mode, args.js)
        except (requests.RequestException, RuntimeError) as e:
            print(f"Scrape failed: {e}", file=sys.stderr)
            return 1

    print(f"URL:          {data['url']}")
    print(f"Status:       {data['status_code']} ({'ok' if data['ok'] else 'error page'})")
    if "fetched_with" in data:
        print(f"Fetched with: {data['fetched_with']}")
    print("-" * 40)
    content = data["content"]
    print(content if isinstance(content, str) else "\n".join(content))
    return 0

if __name__ == "__main__":
    sys.exit(main())
