"""Fetching with a real headless Chromium (Playwright), for sites that block plain requests."""

import logging
import os
import platform
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .blocking import CLEARANCE_HEADERS, get_clearance, has_challenge, save_clearance
from .errors import (
    BrowserBusyError,
    BrowserDisabledError,
    ContentTooLargeError,
    FetchError,
    ScrapeError,
    UnsupportedContentError,
)
from .http_fetch import HTML_CONTENT_TYPES, MAX_CONTENT_BYTES, MAX_REDIRECTS
from .page import Page
from .proxy import context_proxy
from .urls import site_of, validate_host, validate_url

BROWSER_ENABLED = os.environ.get("BROWSER_FALLBACK", "on").lower() != "off"
# Start the browsers in the background at boot, so the first browser request doesn't wait for one.
BROWSER_WARM = os.environ.get("BROWSER_WARM", "on").lower() != "off"
WARM_BARRIER_SECONDS = 30
BROWSER_TIMEOUT_MS = 30000
BROWSER_CHALLENGE_WAIT_SECONDS = 20
BROWSER_RENDER_WAIT_MS = 5000  # extra time for JavaScript to finish building the page
BROWSER_QUEUE_TIMEOUT_SECONDS = 15  # how long a request waits for a free browser
BROWSER_WORKERS = 2
BROWSER_SKIPPED_RESOURCES = {"image", "font", "media"}
RENDER_POLL_MS = 500  # JSON mode: how often the browser checks whether the data is there yet
# Admission control: at most BROWSER_WORKERS pages load at once, the rest wait or get a 503.
browser_slots = threading.BoundedSemaphore(BROWSER_WORKERS)
# Each worker thread owns one Playwright driver and one browser, started on first use and kept open.
# The sync Playwright API is bound to the thread that started it, so the browsers live in these threads.
browser_pool = ThreadPoolExecutor(max_workers=BROWSER_WORKERS, thread_name_prefix="browser")
browser_local = threading.local()

logger = logging.getLogger(__name__)


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
    return f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major_version}.0.0.0 Safari/537.36"


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


def warm_browsers() -> int:
    """Start a browser in every browser worker thread, and say how many started. Each worker must
    start its own (the sync Playwright API is bound to its thread), so every task waits at a barrier
    until all workers hold one, which keeps one worker from taking two tasks. A browser that can't
    start is only logged: the first real request tries again, as it always did."""
    barrier = threading.Barrier(BROWSER_WORKERS, timeout=WARM_BARRIER_SECONDS)

    def warm_one() -> None:
        barrier.wait()
        get_browser()

    started = 0
    for future in [browser_pool.submit(warm_one) for _ in range(BROWSER_WORKERS)]:
        try:
            future.result()
            started += 1
        except Exception as e:
            logger.warning("A browser could not be started ahead of time: %s", first_line(e))
    logger.info("%d of %d browsers started ahead of time", started, BROWSER_WORKERS)
    return started


def start_warming() -> threading.Thread | None:
    """Warm the browsers on a background thread, so the server takes requests right away."""
    if not (BROWSER_ENABLED and BROWSER_WARM):
        return None
    thread = threading.Thread(target=warm_browsers, name="browser-warmup", daemon=True)
    thread.start()
    return thread


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


def wait_out_challenge(tab) -> tuple[bytes, bool]:
    """Poll the page until it stops looking like a bot check, or give up after the wait.
    Also says whether a bot check was seen along the way."""
    deadline = time.monotonic() + BROWSER_CHALLENGE_WAIT_SECONDS
    seen = False
    while True:
        content = read_content(tab)
        if content and not has_challenge(content):
            return content, seen
        seen = seen or bool(content)
        if time.monotonic() >= deadline:
            return content, seen
        tab.wait_for_timeout(1000)


def wait_for_render(tab, content: bytes, ready: Callable[[bytes], bool] | None = None) -> tuple[bytes, bool]:
    """Give JavaScript a bounded amount of time to finish building the page, then re-read it.
    With a `ready` test (JSON mode), stop as soon as the parser can read its data from the page.
    Returns the content and whether the page got its full time (False when it was stopped early)."""
    if ready is None:
        try:
            tab.wait_for_load_state("networkidle", timeout=BROWSER_RENDER_WAIT_MS)
        except PlaywrightTimeoutError:
            logger.info("%s was still loading after %d ms, using what has rendered", tab.url, BROWSER_RENDER_WAIT_MS)
        return read_content(tab) or content, True
    if ready(content):
        return content, False
    deadline = time.monotonic() + BROWSER_RENDER_WAIT_MS / 1000
    while True:
        remaining_ms = (deadline - time.monotonic()) * 1000
        if remaining_ms < 1:  # a timeout of 0 would mean "wait forever" to Playwright
            logger.info("%s was still loading after %d ms, using what has rendered", tab.url, BROWSER_RENDER_WAIT_MS)
            break
        try:
            tab.wait_for_load_state("networkidle", timeout=min(remaining_ms, RENDER_POLL_MS))
            break
        except PlaywrightTimeoutError:
            current = read_content(tab)
            if current and ready(current):
                return current, False
    return read_content(tab) or content, True


def load_in_browser(
    url: str, owner: str | None = None, ready: Callable[[bytes], bool] | None = None, after_block: bool = False
) -> Page:
    """Runs in a browser worker thread. Loads the page in a fresh context and returns it.
    The context starts with any bot-check cookies this owner already earned for the site. When the
    browser gets past a block (after_block) or a bot check, the cookies it ends up with are kept."""
    logger.info("Fetching %s with the browser", url)
    clearance = get_clearance(owner, site_of(url))
    host_cache: dict[str, bool] = {}
    responses = []
    earned = None
    browser = get_browser()
    try:
        context = browser.new_context(
            locale="en-US",
            user_agent=browser_user_agent(browser.version.split(".")[0]),
            service_workers="block",  # service workers would make requests that skip the route check
            proxy=context_proxy(url),  # chosen by the starting URL's site; None goes out directly
        )
    except PlaywrightError as e:
        raise FetchError(f"The browser could not open a page: {first_line(e)}") from e
    try:
        if clearance is not None:
            try:
                context.add_cookies(clearance.cookies)
            except PlaywrightError as e:
                logger.info("Could not reuse the saved cookies for %s: %s", url, first_line(e))
        context.route("**/*", lambda route: route_request(route, host_cache))
        context.route_web_socket("**/*", lambda ws: route_web_socket(ws, host_cache))
        tab = context.new_page()

        def remember_navigation(response):
            if response.frame == tab.main_frame and response.request.is_navigation_request():
                responses.append(response)

        tab.on("response", remember_navigation)

        tab.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
        content, challenge_seen = wait_out_challenge(tab)
        complete = True
        if content and not has_challenge(content):
            content, complete = wait_for_render(tab, content, ready)
        final_url = tab.url
        if responses:
            last = responses[-1]
            status_code = last.status
            content_type = last.headers.get("content-type", "")
            hops = redirect_chain(last.request)
            if owner and (after_block or challenge_seen) and content and not has_challenge(content):
                headers = {k: v for k, v in last.request.all_headers().items() if k in CLEARANCE_HEADERS}
                earned = (context.cookies([final_url]), headers)
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
    # Only now, after every check passed, are the cookies worth keeping.
    if earned is not None:
        save_clearance(owner, site_of(final_url), *earned)
    return Page(final_url, status_code, content, "browser", complete)


def fetch_with_browser(
    url: str,
    *,
    owner: str | None = None,
    ready: Callable[[bytes], bool] | None = None,
    queue_timeout: float = BROWSER_QUEUE_TIMEOUT_SECONDS,
    after_block: bool = False,
) -> Page:
    if not BROWSER_ENABLED:
        raise BrowserDisabledError("Browser fetching is turned off on this server.")
    validate_url(url)
    if not browser_slots.acquire(timeout=queue_timeout):
        raise BrowserBusyError("All browsers are busy right now, try again in a moment.")
    try:
        # .result() re-raises whatever the worker raised, so ScrapeErrors come through unchanged.
        return browser_pool.submit(load_in_browser, url, owner, ready, after_block).result()
    finally:
        browser_slots.release()
