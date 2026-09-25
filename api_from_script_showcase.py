import os
import requests

API_URL = "https://ihabov-universal-scraper.fly.dev/scrape"
API_KEY = os.environ["SCRAPER_API_KEY"]

def scrape(url: str, mode: str = "html", browser: str = "auto") -> dict:
    response = requests.get(
        API_URL,
        params={"url": url, "mode": mode, "browser": browser},
        headers={"X-API-Key": API_KEY},
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"{response.status_code}: {response.json()['detail']}")
    return response.json()

data = scrape("https://www.knjizare-vulkan.rs/domaci-klasici/25486-tvrdava", mode="json")

print(data["url"], data["status_code"], data["fetched_with"])
for key, value in data["parsed"].items():
    print(f"{key}: {value}")
