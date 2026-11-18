"""Every setting, read once from the environment."""

import os
from dataclasses import dataclass

# Practice sites built for scraping: sending them through a paid proxy would only spend traffic.
DEFAULT_PROXY_BYPASS = ("toscrape.com", "webscraper.io", "scrapeme.live", "example.com")


@dataclass
class Settings:
    api_keys: list[str]
    redis_url: str | None
    proxy_url: str | None
    proxy_bypass: tuple[str, ...]
    ip_rate_limit: str
    key_rate_limits: str
    rate_limit_fail_open: bool
    trusted_ip_header: str | None
    browser_enabled: bool
    browser_warm: bool
    browser_engine: str  # "playwright" or "patchright" (same API)
    browser_workers: int
    batch_workers: int  # per API key


def env_list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def env_on(name: str) -> bool:
    return os.environ.get(name, "on").lower() not in ("off", "false", "0", "no")


def load() -> Settings:
    proxy = os.environ.get("PROXY_URL", "").strip()
    return Settings(
        api_keys=env_list("API_KEYS"),
        redis_url=os.environ.get("REDIS_URL") or None,
        # Providers often show the proxy as user:pass@host:port; curl assumes http:// for that too.
        proxy_url=(proxy if "://" in proxy else f"http://{proxy}") if proxy else None,
        proxy_bypass=tuple(h.lower().lstrip(".") for h in env_list("PROXY_BYPASS")) or DEFAULT_PROXY_BYPASS,
        ip_rate_limit=os.environ.get("IP_RATE_LIMIT", "10/minute"),
        key_rate_limits=os.environ.get("KEY_RATE_LIMITS", "300/minute;10000/day"),
        rate_limit_fail_open=env_on("RATE_LIMIT_FAIL_OPEN"),
        # Fly's edge sets Fly-Client-IP; anywhere else a client could send it, so it's trusted only on Fly.
        trusted_ip_header=os.environ.get("TRUSTED_IP_HEADER")
        or ("fly-client-ip" if os.environ.get("FLY_APP_NAME") else None),
        browser_enabled=env_on("BROWSER_FALLBACK"),
        browser_warm=env_on("BROWSER_WARM"),
        browser_engine=os.environ.get("BROWSER_ENGINE", "playwright"),
        browser_workers=int(os.environ.get("BROWSER_WORKERS", "2")),
        batch_workers=int(os.environ.get("BATCH_WORKERS", "8")),
    )


settings = load()
