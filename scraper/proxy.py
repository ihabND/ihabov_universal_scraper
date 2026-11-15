"""The outgoing proxy, if one is configured.

Sites often block or challenge data-center IPs such as a Fly.io machine's. With PROXY_URL set,
both fetch paths (plain HTTP and the browser) send their requests out through the proxy instead:

    PROXY_URL=http://username:password@proxy-host:port

The URL contains a password, so it lives in an environment variable (a Fly secret in production)
and is never logged or returned by the API.
"""

import os
from urllib.parse import unquote, urlsplit


def with_scheme(url: str | None) -> str | None:
    """Providers often show the proxy without a scheme (user:pass@host:port). Without one, the URL
    can't be split into credentials and host, so plain http:// is assumed, as curl itself does."""
    if not url:
        return None
    return url if "://" in url else f"http://{url}"


PROXY_URL = with_scheme(os.environ.get("PROXY_URL", "").strip())


def playwright_proxy(url: str | None) -> dict[str, str] | None:
    """The proxy in the form Playwright's `launch(proxy=...)` takes, or None when there is none.

    Playwright wants the server and the credentials as separate fields:
        {"server": "http://proxy-host:port", "username": "...", "password": "..."}
    """
    if not url:
        return None
    parts = urlsplit(url)
    # Built from the parts, not from .netloc, which would carry the credentials into "server".
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    proxy = {"server": server}
    # urlsplit leaves credentials percent-encoded: "p%40ss" is the password "p@ss".
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy


def proxy_status() -> str:
    """For /health: whether a proxy is in use. Never the URL, which holds a password."""
    return "on" if PROXY_URL else "off"
