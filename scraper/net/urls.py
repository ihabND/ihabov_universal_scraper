"""URL clean-up, and the check that keeps requests away from private and internal addresses."""

import asyncio
import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from ..core.errors import BlockedURLError, HostNotFoundError, InvalidURLError

ALLOWED_SCHEMES = {"http", "https"}
PATH_SAFE_CHARS = "/%:@!$&'()*+,;=~"
# Lookups get their own threads, so they never wait behind parsing in the default pool (5 threads on 1 CPU).
DNS_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="dns")

logger = logging.getLogger(__name__)


def normalize_url(url: str) -> str | None:
    try:
        parts = urlsplit(url.strip())
        port, host = parts.port, parts.hostname
        if parts.scheme.lower() not in ALLOWED_SCHEMES or not host:
            return None
        host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
    except ValueError:
        return None
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = (f"{userinfo}@" if userinfo else "") + host + (f":{port}" if port is not None else "")
    return urlunsplit((parts.scheme.lower(), netloc, quote(parts.path or "/", safe=PATH_SAFE_CHARS), parts.query, ""))


def host_in(host: str, sites) -> bool:
    """The exact host or a real subdomain of one of the sites: \"evil-toscrape.com\" doesn't match \"toscrape.com\"."""
    host = host.lower()
    return any(host == site or host.endswith("." + site) for site in sites)


def site_of(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def with_params(url: str, params: dict[str, str]) -> str:
    """The URL with the site's own query parameters added; they replace ones of the same name."""
    if not params:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in params]
    return urlunsplit(parts._replace(query=urlencode([*query, *params.items()])))


# Second-level names under a country code, like co.uk or com.au: the domain is one label longer there.
COUNTRY_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def domain_of(site: str) -> str:
    """The domain a site is grouped under: books.toscrape.com -> toscrape.com, news.bbc.co.uk -> bbc.co.uk."""
    labels = site.split(".")
    keep = 3 if len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in COUNTRY_SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def is_public(ip: str) -> bool:
    address = ipaddress.ip_address(ip)
    return address.is_global and not address.is_multicast


async def resolve_public(host: str) -> list[str]:
    """The host's addresses, when every one of them is public."""
    try:
        lookup = partial(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
        infos = await asyncio.get_running_loop().run_in_executor(DNS_POOL, lookup)
    except socket.gaierror:
        raise HostNotFoundError(f"Could not resolve {host}.") from None
    ips = [info[4][0] for info in infos]
    if not all(is_public(ip) for ip in ips):
        logger.warning("Blocked a connection to non-public host %s", host)
        raise BlockedURLError("This host is not allowed")
    return ips


async def validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
        raise InvalidURLError("Only HTTP and HTTPS allowed.")
    await resolve_public(parts.hostname)
