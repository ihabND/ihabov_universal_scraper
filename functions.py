from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from bs4 import BeautifulSoup
from urllib.parse import urlsplit, urlunsplit, urljoin, quote
from dataclasses import dataclass
from playwright.sync_api import sync_playwright, Error as PlaywrightError
import logging
import ipaddress
import os
import threading
import time
import socket
from typing import Literal

ALLOWED_SCHEMES = {"http", "https"}
PATH_SAFE_CHARS = "/%:@!$&'()*+,;=~"
REQUEST_TIMEOUT = 10
MAX_REDIRECTS = 5
MAX_RETRIES = 3
RETRY_STATUS_CODES = {429, 502, 503, 504}
BACKOFF_SECONDS = 1
REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_CONTENT_BYTES = 5 * 1024 * 1024
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
BLOCKED_STATUS_CODES = {202, 403, 429, 503}
CHALLENGE_MARKERS = (b"awswaf", b"_cf_chl_opt", b"<title>Just a moment...</title>", b"captcha-delivery.com")
CHALLENGE_PAGE_MAX_BYTES = 150 * 1024
BROWSER_ENABLED = os.environ.get("BROWSER_FALLBACK", "on").lower() != "off"
BROWSER_TIMEOUT_MS = 30000
BROWSER_CHALLENGE_WAIT_SECONDS = 20
BROWSER_SKIPPED_RESOURCES = {"image", "font", "media"}
browser_slots = threading.BoundedSemaphore(2)

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
class BrowserDisabledError(ScrapeError):
    status_code = 400

@dataclass
class Page:
    url: str
    status_code: int
    content: bytes
    fetched_with: Literal["http", "browser"] = "http"

    @property
    def ok(self) -> bool:
        return self.status_code < 400

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

    
def fetch_page(url: str) -> Page:
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(url)
        response = get_with_retries(url)
        if response.status_code not in REDIRECT_CODES:
            check_response(response)
            return Page(url, response.status_code, response.content)
        location = response.headers.get("location")
        if not location:
            return Page(url, response.status_code, response.content)
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

def clean_html_response(response: Page) -> str:
    return BeautifulSoup(response.content, "lxml").prettify()

def has_challenge(content: bytes) -> bool:
    return len(content) <= CHALLENGE_PAGE_MAX_BYTES and any(m in content for m in CHALLENGE_MARKERS)

def looks_blocked(page: Page) -> bool:
    return page.status_code in BLOCKED_STATUS_CODES or has_challenge(page.content)

def route_request(route) -> None:
    request = route.request
    if request.resource_type in BROWSER_SKIPPED_RESOURCES:
        return route.abort()
    try:
        validate_url(request.url)
        if request.resource_type != "document":
            return route.continue_()
        url = request.url
        response = route.fetch(max_redirects=0)
        for _ in range(MAX_REDIRECTS):
            location = response.headers.get("location")
            if response.status not in REDIRECT_CODES or not location:
                break
            url = urljoin(url, location)
            validate_url(url)
            response = route.fetch(url=url, max_redirects=0)
        if url == request.url:
            route.fulfill(response=response)
        else:
            route.fulfill(status=302, headers={"location": url})
    except ScrapeError as e:
        logger.warning("Browser request to %s blocked: %s", request.url, e)
        route.abort()

def fetch_with_browser(url: str) -> Page:
    if not BROWSER_ENABLED:
        raise BrowserDisabledError("Browser fetching is turned off on this server.")
    validate_url(url)
    logger.info("Fetching %s with the browser", url)
    with browser_slots, sync_playwright() as p:
        browser = p.chromium.launch(channel="chromium", headless=True)
        try:
            major = browser.version.split(".")[0]
            context = browser.new_context(
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
                ),
            )
            context.route("**/*", route_request)
            tab = context.new_page()
            statuses = []
            tab.on("response", lambda r: r.frame == tab.main_frame and r.request.is_navigation_request()
                   and statuses.append((r.status, r.headers.get("content-type", ""))))
            tab.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
            content = b""
            deadline = time.monotonic() + BROWSER_CHALLENGE_WAIT_SECONDS
            while time.monotonic() < deadline:
                try:
                    content = tab.content().encode("utf-8")
                except PlaywrightError:
                    content = b""
                if content and not has_challenge(content):
                    break
                tab.wait_for_timeout(1000)
            final_url = tab.url
            validate_url(final_url)
        except PlaywrightError as e:
            raise FetchError(f"The browser could not load the page: {e.message.splitlines()[0]}") from e
        finally:
            browser.close()
    if not content or has_challenge(content):
        raise FetchError("The site's bot protection blocked the browser too.")
    status_code, content_type = statuses[-1] if statuses else (200, "")
    if content_type and not content_type.lower().startswith(HTML_CONTENT_TYPES):
        raise UnsupportedContentError(f"Not an HTML page (Content-Type: {content_type})")
    if len(content) > MAX_CONTENT_BYTES:
        raise ContentTooLargeError("Page is too large.")
    return Page(final_url, status_code, content, "browser")

def fetch(url: str, browser: Literal["auto", "never", "always"] = "auto") -> Page:
    if browser == "always":
        return fetch_with_browser(url)
    use_fallback = browser == "auto" and BROWSER_ENABLED
    try:
        page = fetch_page(url)
    except FetchError as e:
        if not use_fallback:
            raise
        logger.info("HTTP fetch failed for %s (%s), trying the browser", url, e)
        return fetch_with_browser(url)
    if use_fallback and looks_blocked(page):
        logger.info("HTTP fetch of %s looks blocked (status %d), trying the browser", page.url, page.status_code)
        return fetch_with_browser(page.url)
    return page
