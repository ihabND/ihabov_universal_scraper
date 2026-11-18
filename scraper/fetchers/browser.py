"""A real headless Chromium, for sites that block plain requests or build their pages with JavaScript.

`engine` picks the Playwright build: "playwright", or "patchright" (a stealthier fork with the same API).
A second browser tier is another instance with its own name and engine.
"""

import asyncio
import importlib
import logging
import platform
import time
from collections.abc import Callable
from contextlib import suppress

from ..antibot.clearance import CLEARANCE_HEADERS, Clearance
from ..antibot.detect import CAPTCHAS, challenge_of
from ..antibot.solvers import run_solvers
from ..core.errors import BrowserBusyError, ContentTooLargeError, FetchError
from ..core.page import Page
from ..net.guard import EgressGuard
from ..net.proxy import browser_proxy
from ..net.urls import validate_url
from .base import MAX_CONTENT_BYTES, FetchRequest, check_content_type

LOAD_TIMEOUT_MS = 30000
CHALLENGE_WAIT_SECONDS = 20
RENDER_WAIT_MS = 5000  # extra time for JavaScript to finish building the page
RENDER_POLL_MS = 500  # JSON mode: how often to check whether the data is there yet
SKIPPED_RESOURCES = {"image", "font", "media"}
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",  # otherwise navigator.webdriver is true
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",  # WebRTC would bypass the egress guard
]

logger = logging.getLogger(__name__)


class BrowserFetcher:
    renders = True

    def __init__(
        self,
        guard: EgressGuard,
        *,
        name: str = "browser",
        engine: str = "playwright",
        workers: int = 2,
        warm: bool = True,
    ):
        self.guard, self.name, self.warm = guard, name, warm
        self.api = importlib.import_module(f"{engine}.async_api")  # its own error classes too
        self.slots = asyncio.Semaphore(workers)  # pages open at once; the rest wait their queue_timeout
        self.lock = asyncio.Lock()
        self.driver = self.browser = self.warming = None
        self.user_agent = ""
        self.status = "not started"

    async def start(self) -> None:
        if self.warm:  # in the background: the server takes requests while the browser starts
            self.warming = asyncio.create_task(self.get_browser())
            self.warming.add_done_callback(lambda task: task.cancelled() or task.exception())  # logged in get_browser

    async def stop(self) -> None:
        async with self.lock:  # a browser that is still starting is closed once it has started
            if self.browser is not None:
                with suppress(self.api.Error):
                    await self.browser.close()
            if self.driver is not None:
                with suppress(self.api.Error):
                    await self.driver.stop()
            self.driver = self.browser = None

    async def get_browser(self):
        async with self.lock:
            if self.browser is None or not self.browser.is_connected():
                try:
                    if self.driver is None:
                        self.driver = await self.api.async_playwright().start()
                    self.browser = await self.driver.chromium.launch(
                        channel="chromium", headless=True, args=LAUNCH_ARGS
                    )
                except Exception as e:  # a missing executable included
                    self.status = f"failed: {first_line(e)}"
                    logger.warning("The browser could not start: %s", first_line(e))
                    raise FetchError(f"The browser could not start: {first_line(e)}") from e
                self.user_agent = user_agent(self.browser.version.split(".")[0])
                self.status = "ready"
        return self.browser

    async def fetch(self, req: FetchRequest) -> Page:
        await validate_url(req.url)
        try:
            await asyncio.wait_for(self.slots.acquire(), req.queue_timeout)
        except TimeoutError:
            raise BrowserBusyError("All browsers are busy right now, try again in a moment.") from None
        try:
            return await self.load(req)
        finally:
            self.slots.release()

    async def load(self, req: FetchRequest) -> Page:
        logger.info("Fetching %s with the %s", req.url, self.name)
        browser = await self.get_browser()
        try:
            context = await browser.new_context(
                locale="en-US",
                user_agent=self.user_agent,
                service_workers="block",  # their requests would skip the resource filter
                proxy=browser_proxy(req.url, self.guard),
            )
        except self.api.Error as e:
            raise FetchError(f"The browser could not open a page: {first_line(e)}") from e
        try:
            if req.clearance:
                with suppress(self.api.Error):
                    await context.add_cookies(req.clearance.cookies)
            await context.route("**/*", self.skip_heavy)
            tab = await context.new_page()
            responses = []
            tab.on(
                "response",
                lambda r: r.frame == tab.main_frame and r.request.is_navigation_request() and responses.append(r),
            )
            await tab.goto(req.url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS)
            content, challenged = await self.wait_out_challenge(tab)
            complete = True
            if content and not challenge_of(content):
                content, complete = await self.wait_for_render(tab, content, req.ready)
            if not content or not responses:
                raise FetchError("The browser did not get the page.")
            last = responses[-1]
            earned = None
            if keeps_cookies(req.after_block, challenged, content):
                headers = {k: v for k, v in (await last.request.all_headers()).items() if k in CLEARANCE_HEADERS}
                earned = Clearance(await context.cookies([tab.url]), headers)
            page = Page(tab.url, last.status, content, self.name, complete, earned)
            content_type = last.headers.get("content-type", "")
        except self.api.Error as e:
            if "ERR_SOCKS_CONNECTION_FAILED" in str(e):  # the egress guard couldn't connect to the site
                raise FetchError("Could not connect to the site.") from e
            raise FetchError(f"The browser could not load the page: {first_line(e)}") from e
        finally:
            with suppress(self.api.Error):
                await context.close()
        if 200 <= page.status_code < 300:
            check_content_type(content_type)
        if len(page.content) > MAX_CONTENT_BYTES:
            raise ContentTooLargeError("Page is too large.")
        return page

    async def skip_heavy(self, route) -> None:
        with suppress(self.api.Error):  # the page may close while a request is still in flight
            if route.request.resource_type in SKIPPED_RESOURCES:
                await route.abort()
            else:
                await route.continue_()

    async def read_content(self, tab) -> bytes:
        try:
            return (await tab.content()).encode()
        except self.api.Error:  # in the middle of a navigation
            return b""

    async def wait_out_challenge(self, tab) -> tuple[bytes, bool]:
        """Poll until the page stops being a bot check, running solvers once per vendor. Says whether one was seen."""
        deadline = time.monotonic() + CHALLENGE_WAIT_SECONDS
        solved: set[str] = set()
        while True:
            content = await self.read_content(tab)
            vendor = challenge_of(content) if content else None
            if content and vendor is None:
                return content, bool(solved)
            if vendor and vendor not in solved:
                solved.add(vendor)
                if not await run_solvers(tab, vendor) and vendor in CAPTCHAS:
                    return content, True  # nothing here can pass it, so waiting would only cost time
            if time.monotonic() >= deadline:
                return content, bool(solved)
            await tab.wait_for_timeout(1000)

    async def wait_for_render(self, tab, content: bytes, ready: Callable[[bytes], bool] | None) -> tuple[bytes, bool]:
        """Give JavaScript a bounded time to finish the page. With `ready` (JSON mode), stop as soon as the
        parser can read its data. Also says whether the page got its full time."""
        deadline = time.monotonic() + RENDER_WAIT_MS / 1000
        if ready is not None and await asyncio.to_thread(ready, content):
            return content, False
        while (remaining_ms := (deadline - time.monotonic()) * 1000) >= 1:  # 0 would mean "forever" to Playwright
            try:
                await tab.wait_for_load_state("networkidle", timeout=min(remaining_ms, RENDER_POLL_MS))
                break
            except self.api.TimeoutError:
                current = await self.read_content(tab)
                if ready is not None and current and await asyncio.to_thread(ready, current):
                    return current, False
        return await self.read_content(tab) or content, True


def keeps_cookies(after_block: bool, challenged: bool, content: bytes) -> bool:
    """Cookies are worth keeping when they got this page past a block or a bot check it ended up passing."""
    return (after_block or challenged) and not challenge_of(content)


def user_agent(major_version: str) -> str:
    """A Chrome user agent for the OS this runs on, so it matches the client hints Chromium sends itself."""
    os_part = {"Windows": "Windows NT 10.0; Win64; x64", "Darwin": "Macintosh; Intel Mac OS X 10_15_7"}
    system = os_part.get(platform.system(), "X11; Linux x86_64")
    return f"Mozilla/5.0 ({system}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major_version}.0.0.0 Safari/537.36"


def first_line(error: BaseException) -> str:
    return (getattr(error, "message", None) or str(error) or type(error).__name__).splitlines()[0]
