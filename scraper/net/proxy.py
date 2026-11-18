"""Which proxy a request goes out through: the configured upstream proxy, or the local egress guard."""

from urllib.parse import unquote, urlsplit

from ..settings import settings
from .guard import EgressGuard


def upstream_for(url: str) -> str | None:
    if not settings.proxy_url:
        return None
    host = urlsplit(url).hostname or ""
    # Exact host or a real subdomain: "evil-toscrape.com" must not match "toscrape.com".
    if any(host == site or host.endswith("." + site) for site in settings.proxy_bypass):
        return None
    return settings.proxy_url


def curl_proxy(url: str, guard: EgressGuard) -> str:
    return upstream_for(url) or f"socks5h://{guard.address}"  # socks5h: the guard resolves the host


def browser_proxy(url: str, guard: EgressGuard) -> dict[str, str]:
    upstream = upstream_for(url)
    return playwright_proxy(upstream) if upstream else {"server": f"socks5://{guard.address}"}


def playwright_proxy(url: str) -> dict[str, str]:
    """Playwright wants the server and the (percent-decoded) credentials as separate fields."""
    parts = urlsplit(url)
    proxy = {"server": f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy


def proxy_status() -> str:
    return "on" if settings.proxy_url else "off"
