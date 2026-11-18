"""Plain HTTP with a real browser's TLS fingerprint (curl_cffi)."""

import asyncio
import logging
import time
from urllib.parse import urljoin

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession, Cookies, Response
from curl_cffi.requests.exceptions import RequestException

from ..antibot.detect import challenge_of
from ..core.errors import ContentTooLargeError, FetchError
from ..core.page import Page
from ..net.guard import EgressGuard
from ..net.proxy import curl_proxy
from ..net.urls import validate_url
from .base import MAX_CONTENT_BYTES, FetchRequest, check_content_type

REQUEST_TIMEOUT = 10
FETCH_BUDGET_SECONDS = 25  # one fetch, including retries, waits and redirects
MAX_REDIRECTS = 5
MAX_RETRIES = 2
BACKOFF_SECONDS = 1
RETRY_STATUS_CODES = {429, 502, 503, 504}
REDIRECT_CODES = {301, 302, 303, 307, 308}
CURLE_FILESIZE_EXCEEDED = 63

logger = logging.getLogger(__name__)


class HttpFetcher:
    renders = False
    status = "ready"

    def __init__(
        self, guard: EgressGuard, *, name: str = "http", impersonate: str = "chrome", retries: int = MAX_RETRIES
    ):
        self.guard, self.name, self.impersonate, self.retries = guard, name, impersonate, retries
        self.session: AsyncSession | None = None

    async def start(self) -> None:
        # discard_cookies: one session serves every caller, so cookies are passed per fetch instead.
        self.session = AsyncSession(
            impersonate=self.impersonate,
            discard_cookies=True,
            curl_options={
                CurlOpt.CONNECTTIMEOUT_MS: REQUEST_TIMEOUT * 1000,
                CurlOpt.LOW_SPEED_LIMIT: 1,  # a connection that sends nothing for 10 s is stalled
                CurlOpt.LOW_SPEED_TIME: REQUEST_TIMEOUT,
                CurlOpt.MAXFILESIZE_LARGE: MAX_CONTENT_BYTES,
            },
        )

    async def stop(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def fetch(self, req: FetchRequest) -> Page:
        """Follows redirects by hand, so every hop is checked before it's requested."""
        if self.session is None:
            await self.start()
        cookies, headers = Cookies(), None
        if req.clearance and req.clearance.plain_ok:
            for c in req.clearance.cookies:
                cookies.set(
                    c["name"], c["value"], c.get("domain", ""), c.get("path") or "/", secure=bool(c.get("secure"))
                )
            headers = req.clearance.headers
        deadline = time.monotonic() + FETCH_BUDGET_SECONDS
        url = req.url
        for _ in range(MAX_REDIRECTS + 1):
            await validate_url(url)
            response = await self.get(url, cookies, headers, deadline)
            keep_cookies(cookies, response)
            location = response.headers.get("location")
            if response.status_code in REDIRECT_CODES and location:
                url = urljoin(url, location)
                continue
            if 200 <= response.status_code < 300:
                check_content_type(response.headers.get("content-type", ""))
            return Page(url, response.status_code, response.content, self.name)
        raise FetchError("Too many redirects")

    async def get(self, url: str, cookies: Cookies, headers: dict | None, deadline: float) -> Response:
        for attempt in range(self.retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("The site took too long to respond.")
            response = error = None
            try:
                response = await self.session.get(
                    url,
                    timeout=remaining,
                    allow_redirects=False,
                    cookies=cookies,
                    headers=headers,
                    proxy=curl_proxy(url, self.guard),
                )
            except RequestException as e:
                if getattr(e, "code", None) == CURLE_FILESIZE_EXCEEDED:
                    raise ContentTooLargeError("Page is too large.") from None
                error = e
            # A bot check isn't a temporary error: retrying only delays the next fetcher.
            if response is not None and (
                response.status_code not in RETRY_STATUS_CODES or challenge_of(response.content)
            ):
                return response
            wait = BACKOFF_SECONDS * 2**attempt
            if attempt == self.retries or time.monotonic() + wait + 1 > deadline:
                if response is not None:
                    return response
                raise FetchError(f"Could not fetch the page: {error}") from error
            logger.info(
                "Attempt %d for %s failed (%s), retrying", attempt + 1, url, response.status_code if response else error
            )
            await asyncio.sleep(wait)


def keep_cookies(jar: Cookies, response: Response) -> None:
    """Keep what a response sets for the rest of this fetch. Read here because curl_cffi drops cookies with
    an expiry date it can't parse (Goodreads' "-0000" dates) and leaves the path empty when there is none."""
    for header in response.headers.get_list("set-cookie"):
        pair, *attributes = header.split(";")
        name, _, value = pair.partition("=")
        options = {key.strip().lower(): val.strip() for key, _, val in (a.partition("=") for a in attributes)}
        jar.set(name.strip(), value.strip(), options.get("domain", ""), options.get("path") or "/", "secure" in options)
