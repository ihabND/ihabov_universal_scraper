from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from bs4 import BeautifulSoup
from urllib.parse import urlsplit, urlunsplit, urljoin, quote
import logging
import ipaddress
import time
import socket
from typing import Literal

ALLOWED_SCHEMES = {"http", "https"}
PATH_SAFE_CHARS = "/%:@!$&'()*+,;=~"
NON_CONTENT_TAGS = ["script", "style", "noscript", "template"]
REQUEST_TIMEOUT = 10
MAX_REDIRECTS = 5
MAX_RETRIES = 3
RETRY_STATUS_CODES = {429, 502, 503, 504}
BACKOFF_SECONDS = 1
REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_CONTENT_BYTES = 5 * 1024 * 1024
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

session = requests.Session(impersonate="chrome")

logger = logging.getLogger(__name__)

class ScrapeError(Exception):
    status_code = 502
class InvalidURLError(ScrapeError):
    status_code = 400
class BlockedURLError(ScrapeError):
    status_code = 403
class ContentTooLargeError(ScrapeError):
    status_code = 413
class UnsupportedContentError(ScrapeError):
    status_code = 415
class FetchError(ScrapeError):
    status_code = 502
class UnsupportedSiteError(ScrapeError):
    status_code = 400
class ParseError(ScrapeError):
    status_code = 502

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

def is_public_host(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for family, type_, proto, canonname, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True

def validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
        raise InvalidURLError("Only HTTP and HTTPS allowed.")
    if not is_public_host(parts.hostname):
        logger.warning("Blocked request to non-public host: %s", parts.hostname)
        raise BlockedURLError("This host is not allowed")

def check_response(response: requests.Response) -> None:
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not content_type.startswith(HTML_CONTENT_TYPES):
        raise UnsupportedContentError(f"Not an HTML page (Content-Type: {content_type})")
    declared_length = response.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > MAX_CONTENT_BYTES:
        raise ContentTooLargeError("Page is too large.")        
    if len(response.content) > MAX_CONTENT_BYTES:
        raise ContentTooLargeError("Page is too large.")

    
def fetch_page(url: str) -> requests.Response:
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(url)
        response = get_with_retries(url)
        if response.status_code not in REDIRECT_CODES:
            check_response(response)
            return response
        location = response.headers.get("location")
        if not location:
            return response
        url = urljoin(url, location)
        logger.info("Following redirect to %s", url)
    raise FetchError("Too many redirects")

def get_with_retries(url: str) -> requests.Response:
    for attempt in range(MAX_RETRIES + 1):
        is_last_attempt = attempt == MAX_RETRIES
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        except RequestException as e:
            if is_last_attempt:
                raise FetchError(f"Could not fetch the page: {e}") from e
            logger.info("Attempt %d failed for %s: %s", attempt + 1, url, e)
        else:
            if response.status_code not in RETRY_STATUS_CODES or is_last_attempt:
                return response
            logger.info("Got %d from %s, retrying", response.status_code, url)
        time.sleep(BACKOFF_SECONDS * 2 ** attempt)

def clean_html_response(
    response: requests.Response,
    mode: Literal["text", "html"] = "text",
) -> str | list[str]:
    content = BeautifulSoup(response.content, "lxml")
    if mode == "html":
        return content.prettify()
    for tag in content(NON_CONTENT_TAGS):
        tag.decompose()
    text = content.get_text(separator="\n")
    result = [line.strip() for line in text.splitlines() if line.strip()]
    return result
