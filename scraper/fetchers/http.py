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
from ..core.strategy import Strategy
from ..net.guard import EgressGuard
from ..net.proxy import curl_proxy
from ..net.urls import validate_url
from .base import MAX_CONTENT_BYTES, FetchRequest, check_content_type

MAX_REDIRECTS = 5
BACKOFF_SECONDS = 1
RETRY_STATUS_CODES = {429, 502, 503, 504}
REDIRECT_CODES = {301, 302, 303, 307, 308}
CURLE_FILESIZE_EXCEEDED = 63
CURLE_PROXY = 97  # the egress guard couldn't connect to the site

logger = logging.getLogger(__name__)


class HttpFetcher:
    renders = False
    status = "ready"

    def __init__(
        self, guard: EgressGuard, *, name: str = "http", impersonate: str = "chrome", retries: int | None = None
    ):  # retries=None: as many as the site's strategy says, which a site that starts here always gets
        self.guard, self.name, self.impersonate, self.retries = guard, name, impersonate, retries
        self.session: AsyncSession | None = None

    async def start(self) -> None:
        # discard_cookies: one session serves every caller, so cookies are passed per fetch instead.
        self.session = AsyncSession(
            impersonate=self.impersonate,
            discard_cookies=True,
            # Timeouts are set per request: session options would override them.
            curl_options={CurlOpt.MAXFILESIZE_LARGE: MAX_CONTENT_BYTES},
        )

    async def stop(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def fetch(self, req: FetchRequest) -> Page:
        """Follows redirects by hand, so every hop is checked before it's requested."""
        if self.session is None:
            await self.start()
        strategy, cookies = req.strategy, Cookies()
        headers = {} if strategy.locale == "en-US" else {"accept-language": accept_language(strategy.locale)}
        headers.update(strategy.headers)
        if req.clearance and req.clearance.plain_ok:
            for c in req.clearance.cookies:
                cookies.set(
                    c["name"], c["value"], c.get("domain", ""), c.get("path") or "/", secure=bool(c.get("secure"))
                )
            headers.update(req.clearance.headers)  # the cookies only work with the headers they were earned with
        deadline = time.monotonic() + strategy.fetch_budget
        url = req.url
        for _ in range(MAX_REDIRECTS + 1):
            await validate_url(url)
            response = await self.get(url, cookies, headers or None, deadline, strategy)
            keep_cookies(cookies, response)
            location = response.headers.get("location")
            if response.status_code in REDIRECT_CODES and location:
                url = urljoin(url, location)
                continue
            if 200 <= response.status_code < 300:
                check_content_type(response.headers.get("content-type", ""))
            return Page(url, response.status_code, response.content, self.name)
        raise FetchError("Too many redirects")

    async def get(
        self, url: str, cookies: Cookies, headers: dict | None, deadline: float, strategy: Strategy
    ) -> Response:
        retries = strategy.retries if self.retries is None or self.name == strategy.start else self.retries
        for attempt in range(retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("The site took too long to respond.")
            response = error = None
            try:
                response = await self.session.get(
                    url,
                    timeout=(connect := min(strategy.request_timeout, remaining), remaining - connect),
                    allow_redirects=False,
                    cookies=cookies,
                    headers=headers,
                    proxy=curl_proxy(url, self.guard, strategy.proxy),
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
            if attempt == retries or time.monotonic() + wait + 1 > deadline:
                if response is not None:
                    return response
                if getattr(error, "code", None) == CURLE_PROXY:
                    raise FetchError("Could not connect to the site.") from error
                raise FetchError(f"Could not fetch the page: {error}") from error
            logger.info(
                "Attempt %d for %s failed (%s), retrying", attempt + 1, url, response.status_code if response else error
            )
            await asyncio.sleep(wait)


def accept_language(locale: str) -> str:
    language = locale.split("-")[0]
    return f"{locale},{language};q=0.9" if language != locale else locale


def keep_cookies(jar: Cookies, response: Response) -> None:
    """Keep what a response sets for the rest of this fetch. Read here because curl_cffi drops cookies with
    an expiry date it can't parse (Goodreads' "-0000" dates) and leaves the path empty when there is none."""
    for header in response.headers.get_list("set-cookie"):
        pair, *attributes = header.split(";")
        name, _, value = pair.partition("=")
        options = {key.strip().lower(): val.strip() for key, _, val in (a.partition("=") for a in attributes)}
        jar.set(name.strip(), value.strip(), options.get("domain", ""), options.get("path") or "/", "secure" in options)
