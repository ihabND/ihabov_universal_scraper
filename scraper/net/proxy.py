"""Which proxy a request goes out through: the configured upstream proxy, or the local egress guard."""

from urllib.parse import unquote, urlsplit

from ..settings import settings
from .guard import EgressGuard
from .urls import host_in


def upstream_for(url: str, mode: str = "default") -> str | None:
    """`mode` is the site's choice: direct, proxy (when one is configured), or default (PROXY_BYPASS decides)."""
    if not settings.proxy_url or mode == "direct":
        return None
    if mode == "proxy":
        return settings.proxy_url
    if host_in(urlsplit(url).hostname or "", settings.proxy_bypass):
        return None
    return settings.proxy_url


def curl_proxy(url: str, guard: EgressGuard, mode: str = "default") -> str:
    return upstream_for(url, mode) or f"socks5h://{guard.address}"  # socks5h: the guard resolves the host


def browser_proxy(url: str, guard: EgressGuard, mode: str = "default") -> dict[str, str]:
    upstream = upstream_for(url, mode)
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
