from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from bs4 import BeautifulSoup
from urllib.parse import urlsplit, urlunsplit, urljoin, quote
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import (
    sync_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)
import logging
import ipaddress
import os
import platform
import threading
import time
import socket
from typing import Literal

ALLOWED_SCHEMES = {"http", "https"}
PATH_SAFE_CHARS = "/%:@!$&'()*+,;=~"
REQUEST_TIMEOUT = 10
FETCH_BUDGET_SECONDS = 25  # total time for one plain fetch: all retries, backoff waits and redirect hops
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
BROWSER_RENDER_WAIT_MS = 5000       # extra time for JavaScript to finish building the page
BROWSER_QUEUE_TIMEOUT_SECONDS = 15  # how long a request waits for a free browser
BROWSER_WORKERS = 2
BROWSER_SKIPPED_RESOURCES = {"image", "font", "media"}
# Admission control: at most BROWSER_WORKERS pages load at once, the rest wait or get a 503.
browser_slots = threading.BoundedSemaphore(BROWSER_WORKERS)
# Each worker thread owns one Playwright driver and one browser, started on first use and kept open.
# The sync Playwright API is bound to the thread that started it, so the browsers live in these threads.
browser_pool = ThreadPoolExecutor(max_workers=BROWSER_WORKERS, thread_name_prefix="browser")
browser_local = threading.local()

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
class BrowserBusyError(ScrapeError):
    status_code = 503

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
        for family, type_, proto, canonname, sockaddr in infos:
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

def check_headers(response: requests.Response) -> None:
    """Reject the response from its headers, before more than the first chunk of body is downloaded."""
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not content_type.startswith(HTML_CONTENT_TYPES):
        raise UnsupportedContentError(f"Not an HTML page (Content-Type: {content_type})")
    declared_length = response.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > MAX_CONTENT_BYTES:
        raise ContentTooLargeError("Page is too large.")

def read_body(response: requests.Response, deadline: float) -> bytes:
    """Download the streamed body, stopping as soon as it goes over the size limit or the
    fetch budget. (In streaming mode curl's timeout only catches a stalled connection,
    not a body that trickles in slowly, so the deadline is checked here as well.)"""
    chunks, size = [], 0
    try:
        for chunk in response.iter_content():
            size += len(chunk)
            if size > MAX_CONTENT_BYTES:
                raise ContentTooLargeError("Page is too large.")
            if time.monotonic() > deadline:
                raise FetchError("The site took too long to send the page.")
            chunks.append(chunk)
    except RequestException as e:
        raise FetchError(f"Could not fetch the page: {e}") from e
    return b"".join(chunks)

def fetch_page(url: str) -> Page:
    deadline = time.monotonic() + FETCH_BUDGET_SECONDS
    # One session per fetch: cookies set along this redirect chain are kept for it,
    # and never sent on anyone else's request.
    with requests.Session(impersonate="chrome") as session:
        for _ in range(MAX_REDIRECTS + 1):
            validate_url(url)
            response = get_with_retries(session, url, deadline)
            try:
                location = response.headers.get("location")
                if response.status_code in REDIRECT_CODES and location:
                    url = urljoin(url, location)
                    logger.info("Following redirect to %s", url)
                    continue
                if response.status_code not in REDIRECT_CODES:
                    check_headers(response)
                return Page(url, response.status_code, read_body(response, deadline))
            finally:
                response.close()
    raise FetchError("Too many redirects")

def get_with_retries(session: requests.Session, url: str, deadline: float) -> requests.Response:
    """GET the URL with headers only (the body is streamed later), retrying on transient
    failures as long as the next attempt still fits inside the fetch budget."""
    for attempt in range(MAX_RETRIES + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("The site took too long to respond.")
        response, error = None, None
        try:
            response = session.get(url, timeout=min(REQUEST_TIMEOUT, remaining),
                                   allow_redirects=False, stream=True)
        except RequestException as e:
            error = e
        else:
            if response.status_code not in RETRY_STATUS_CODES:
                return response
        wait = BACKOFF_SECONDS * 2 ** attempt
        # Stop when this was the last attempt, or when the wait plus a short request would overrun the budget.
        if attempt == MAX_RETRIES or time.monotonic() + wait + 1 > deadline:
            if response is not None:
                return response
            raise FetchError(f"Could not fetch the page: {error}") from error
        if response is not None:
            logger.info("Got %d from %s, retrying", response.status_code, url)
            response.close()
        else:
            logger.info("Attempt %d failed for %s: %s", attempt + 1, url, error)
        time.sleep(wait)

def clean_html_response(response: Page) -> str:
    return BeautifulSoup(response.content, "lxml").prettify()

def has_challenge(content: bytes) -> bool:
    return len(content) <= CHALLENGE_PAGE_MAX_BYTES and any(m in content for m in CHALLENGE_MARKERS)

def looks_blocked(page: Page) -> bool:
    return page.status_code in BLOCKED_STATUS_CODES or has_challenge(page.content)

def first_line(error: BaseException) -> str:
    text = getattr(error, "message", None) or str(error) or type(error).__name__
    return text.splitlines()[0]

def browser_user_agent(major_version: str) -> str:
    """A Chrome user agent for the OS we actually run on, so it matches the client hints
    (sec-ch-ua-platform, navigator.platform) that Chromium sends on its own."""
    system = platform.system()
    if system == "Windows":
        os_part = "Windows NT 10.0; Win64; x64"
    elif system == "Darwin":
        os_part = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        os_part = "X11; Linux x86_64"
    return (f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major_version}.0.0.0 Safari/537.36")

def get_browser():
    """This worker thread's browser. Started on first use, and started again if it crashed."""
    browser = getattr(browser_local, "browser", None)
    if browser is not None and browser.is_connected():
        return browser
    try:
        if getattr(browser_local, "playwright", None) is None:
            browser_local.playwright = sync_playwright().start()
        logger.info("Starting a browser in %s", threading.current_thread().name)
        browser_local.browser = browser_local.playwright.chromium.launch(
            channel="chromium",
            headless=True,
            # Without this Chromium sets navigator.webdriver = true, which bot checks look for.
            args=["--disable-blink-features=AutomationControlled"],
        )
    except Exception as e:
        raise FetchError(f"The browser could not start: {first_line(e)}") from e
    return browser_local.browser

def route_request(route, host_cache: dict[str, bool]) -> None:
    """Called for every request the page makes. Skips heavy resources, blocks non-public hosts,
    and lets everything else go out through Chromium's own network stack."""
    request = route.request
    try:
        if request.resource_type in BROWSER_SKIPPED_RESOURCES:
            route.abort()
            return
        validate_url(request.url, host_cache)
        route.continue_()
    except ScrapeError as e:
        logger.warning("Browser request to %s blocked: %s", request.url, e)
        try:
            route.abort()
        except PlaywrightError:
            pass
    except PlaywrightError as e:
        # The page or context was closed while this request was in flight. Nothing left to do.
        logger.debug("Route for %s could not be handled: %s", request.url, first_line(e))

def route_web_socket(ws, host_cache: dict[str, bool]) -> None:
    """WebSockets bypass page routing, so check their host here. A blocked socket is simply
    never connected to the server."""
    try:
        validate_host(ws.url, host_cache)
    except ScrapeError as e:
        logger.warning("Browser WebSocket to %s blocked: %s", ws.url, e)
        return
    ws.connect_to_server()

def redirect_chain(request) -> list[str]:
    """The URLs that led to `request`, oldest first, not including the request itself."""
    urls = []
    while request.redirected_from is not None:
        request = request.redirected_from
        urls.append(request.url)
    urls.reverse()
    return urls

def read_content(tab) -> bytes:
    try:
        return tab.content().encode("utf-8")
    except PlaywrightError:
        # The page is in the middle of a navigation, try again later.
        return b""

def wait_out_challenge(tab) -> bytes:
    """Poll the page until it stops looking like a bot check, or give up after the wait."""
    deadline = time.monotonic() + BROWSER_CHALLENGE_WAIT_SECONDS
    while True:
        content = read_content(tab)
        if content and not has_challenge(content):
            return content
        if time.monotonic() >= deadline:
            return content
        tab.wait_for_timeout(1000)

def wait_for_render(tab, content: bytes) -> bytes:
    """Give JavaScript a bounded amount of time to finish building the page, then re-read it."""
    try:
        tab.wait_for_load_state("networkidle", timeout=BROWSER_RENDER_WAIT_MS)
    except PlaywrightTimeoutError:
        logger.info("%s was still loading after %d ms, using what has rendered", tab.url, BROWSER_RENDER_WAIT_MS)
    return read_content(tab) or content

def load_in_browser(url: str) -> Page:
    """Runs in a browser worker thread. Loads the page in a fresh context and returns it."""
    logger.info("Fetching %s with the browser", url)
    host_cache: dict[str, bool] = {}
    responses = []
    browser = get_browser()
    try:
        context = browser.new_context(
            locale="en-US",
            user_agent=browser_user_agent(browser.version.split(".")[0]),
            service_workers="block",  # service workers would make requests that skip the route check
        )
    except PlaywrightError as e:
        raise FetchError(f"The browser could not open a page: {first_line(e)}") from e
    try:
        context.route("**/*", lambda route: route_request(route, host_cache))
        context.route_web_socket("**/*", lambda ws: route_web_socket(ws, host_cache))
        tab = context.new_page()

        def remember_navigation(response):
            if response.frame == tab.main_frame and response.request.is_navigation_request():
                responses.append(response)
        tab.on("response", remember_navigation)

        tab.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        content = wait_out_challenge(tab)
        if content and not has_challenge(content):
            content = wait_for_render(tab, content)
        final_url = tab.url
        if responses:
            last = responses[-1]
            status_code = last.status
            content_type = last.headers.get("content-type", "")
            hops = redirect_chain(last.request)
    except PlaywrightError as e:
        raise FetchError(f"The browser could not load the page: {first_line(e)}") from e
    finally:
        try:
            context.close()
        except PlaywrightError:
            pass
    if not content or has_challenge(content):
        raise FetchError("The site's bot protection blocked the browser too.")
    if not responses:
        raise FetchError("The browser did not get a response from the page.")
    # Chromium follows redirects itself, so the route handler only saw the first URL of each chain.
    # Check the hops afterwards, and refuse the result if any of them was not allowed.
    if len(hops) > MAX_REDIRECTS:
        raise FetchError("Too many redirects")
    for hop in hops:
        validate_url(hop, host_cache)
    validate_url(final_url, host_cache)
    if content_type and not content_type.lower().startswith(HTML_CONTENT_TYPES):
        raise UnsupportedContentError(f"Not an HTML page (Content-Type: {content_type})")
    if len(content) > MAX_CONTENT_BYTES:
        raise ContentTooLargeError("Page is too large.")
    return Page(final_url, status_code, content, "browser")

def fetch_with_browser(url: str) -> Page:
    if not BROWSER_ENABLED:
        raise BrowserDisabledError("Browser fetching is turned off on this server.")
    validate_url(url)
    if not browser_slots.acquire(timeout=BROWSER_QUEUE_TIMEOUT_SECONDS):
        raise BrowserBusyError("All browsers are busy right now, try again in a moment.")
    try:
        # .result() re-raises whatever the worker raised, so ScrapeErrors come through unchanged.
        return browser_pool.submit(load_in_browser, url).result()
    finally:
        browser_slots.release()

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
