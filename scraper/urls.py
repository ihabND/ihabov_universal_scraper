"""URL clean-up, and the check that keeps requests away from private and internal addresses."""

import ipaddress
import logging
import socket
from urllib.parse import quote, urlsplit, urlunsplit

from .errors import BlockedURLError, InvalidURLError

ALLOWED_SCHEMES = {"http", "https"}
PATH_SAFE_CHARS = "/%:@!$&'()*+,;=~"

logger = logging.getLogger(__name__)


def normalize_url(url: str) -> str | None:
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        logger.warning("Invalid URL %s", url)
        return None
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        logger.warning("Invalid URL %s", url)
        return None
    host = parts.hostname
    if not host:
        logger.warning("Invalid URL %s", url)
        return None
    if ":" in host:
        host = f"[{host}]"
    else:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    userinfo = ""
    if "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0] + "@"
    netloc = userinfo + host
    if port is not None:
        netloc += f":{port}"
    path = quote(parts.path or "/", safe=PATH_SAFE_CHARS)
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def is_public_host(hostname: str, cache: dict[str, bool] | None = None) -> bool:
    """Resolve the host and check every address is public. `cache` remembers answers per hostname,
    so one browser page load resolves each host once instead of once per request it makes."""
    if cache is not None and hostname in cache:
        return cache[hostname]
    public = True
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        public = False
    else:
        for *_, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if not ip.is_global or ip.is_multicast:
                public = False
                break
    if cache is not None:
        cache[hostname] = public
    return public


def validate_host(url: str, cache: dict[str, bool] | None = None) -> None:
    hostname = urlsplit(url).hostname
    if not hostname:
        raise InvalidURLError("The URL has no host.")
    if not is_public_host(hostname, cache):
        logger.warning("Blocked request to non-public host: %s", hostname)
        raise BlockedURLError("This host is not allowed")


def validate_url(url: str, cache: dict[str, bool] | None = None) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
        raise InvalidURLError("Only HTTP and HTTPS allowed.")
    validate_host(url, cache)


def site_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")
