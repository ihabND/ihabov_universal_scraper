"""Calling the API from Python. Needs `pip install requests` and SCRAPER_API_KEY in the environment."""

import os

import requests

API_URL = "https://ihabov-universal-scraper.fly.dev/scrape"


def scrape(url: str, mode: str = "html", browser: str = "auto") -> dict:
    response = requests.get(
        API_URL,
        params={"url": url, "mode": mode, "browser": browser},
        headers={"X-API-Key": os.environ["SCRAPER_API_KEY"]},
        timeout=120,  # covers the plain fetch budget plus a full browser run
    )
    if not response.ok:
        raise RuntimeError(f"{response.status_code}: {response.json().get('detail')}")
    return response.json()


if __name__ == "__main__":
    data = scrape("https://www.knjizare-vulkan.rs/domaci-klasici/316418-dervis-i-smrt", mode="json")
    print(data["url"], data["status_code"], data["fetched_with"])
    for key, value in data["parsed"].items():
        print(f"{key}: {value}")
