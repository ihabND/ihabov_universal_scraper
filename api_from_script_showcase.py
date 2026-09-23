import os
import requests

response = requests.get(
    "https://ihabov-universal-scraper.fly.dev/scrape",
    params={"url": "https://www.knjizare-vulkan.rs/domaci-klasici/25486-tvrdava", "mode": "text"},
    headers={"X-API-Key": os.environ["SCRAPER_API_KEY"]},
    timeout=30,
)
response.raise_for_status()

data = response.json()
print(data["status_code"])
for line in data["content"]:
    print(line)