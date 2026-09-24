import os
import requests

API_URL = "https://ihabov-universal-scraper.fly.dev/scrape"
API_KEY = os.environ["SCRAPER_API_KEY"]

def scrape(url: str, mode: str = "text", use_js: bool = False) -> dict:
    response = requests.get(
        API_URL,
        params={"url": url, "mode": mode, "js": use_js},
        headers={"X-API-Key": API_KEY},
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"{response.status_code}: {response.json()['detail']}")
    return response.json()

data = scrape("https://www.knjizare-vulkan.rs/domaci-klasici/25486-tvrdava")

print(data["url"], data["status_code"])
for line in data["content"]:
    print(line)
