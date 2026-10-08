"""Fetching over plain HTTP with a real browser's TLS fingerprint (curl_cffi)."""

import logging
import threading
import time
from urllib.parse import urljoin

from curl_cffi import CurlInfo, CurlOpt, requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests.exceptions import RequestException

from .blocking import Clearance
from .errors import ContentTooLargeError, FetchError, ScrapeError, UnsupportedContentError
from .page import Page
from .urls import validate_url

REQUEST_TIMEOUT = 10
FETCH_BUDGET_SECONDS = 25  # total time for one plain fetch: all retries, backoff waits and redirect hops
MAX_REDIRECTS = 5
MAX_RETRIES = 3
RETRY_STATUS_CODES = {429, 502, 503, 504}
BACKOFF_SECONDS = 1
REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_CONTENT_BYTES = 5 * 1024 * 1024
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

logger = logging.getLogger(__name__)


def check_type_and_length(content_type: str, declared_length: int | None) -> None:
    content_type = content_type.lower()
    if content_type and not content_type.startswith(HTML_CONTENT_TYPES):
        raise UnsupportedContentError(f"Not an HTML page (Content-Type: {content_type})")
    if declared_length is not None and declared_length > MAX_CONTENT_BYTES:
        raise ContentTooLargeError("Page is too large.")


def check_headers(response: requests.Response) -> None:
    declared = response.headers.get("content-length")
    check_type_and_length(
        response.headers.get("content-type", ""), int(declared) if declared and declared.isdigit() else None
    )


# ---- Plain HTTP ----------------------------------------------------------------------------

http_local = threading.local()


def http_session() -> requests.Session:
    """This thread's HTTP session. Reusing it keeps TCP and TLS connections open between fetches,
    so later requests to the same site skip the handshake. Cookies are wiped at the start and end
    of every fetch (see fetch_page), so nothing carries over from one caller to the next."""
    session = getattr(http_local, "session", None)
    if session is None:
        session = requests.Session(
            impersonate="chrome",
            # The same limits the streaming version had: 10 s to connect, and a connection that
            # sends nothing for 10 s counts as stalled. The total per request is the fetch budget.
            curl_options={
                CurlOpt.CONNECTTIMEOUT_MS: REQUEST_TIMEOUT * 1000,
                CurlOpt.LOW_SPEED_LIMIT: 1,
                CurlOpt.LOW_SPEED_TIME: REQUEST_TIMEOUT,
            },
        )
        http_local.session = session
    return session


class BodyReader:
    """Receives a response body from curl chunk by chunk, on the session's own connection (a
    streamed response would get a throwaway connection). Before keeping the first chunk it checks
    the status and headers, so a non-HTML or oversized page is dropped before the rest arrives.
    An exception can't cross back through curl's C callback, so a rejection is stored in `error`
    and curl is told to stop."""

    def __init__(self, curl):
        self.curl = curl
        self.chunks: list[bytes] = []
        self.size = 0
        self.checked = False
        self.error: ScrapeError | None = None

    def __call__(self, chunk: bytes) -> int:
        try:
            if not self.checked:
                self.checked = True
                status = self.curl.getinfo(CurlInfo.RESPONSE_CODE)
                # Redirect bodies are thrown away and retryable errors may be retried, so only a
                # page we would actually return is checked here. fetch_page checks it again after.
                if status not in REDIRECT_CODES and status not in RETRY_STATUS_CODES:
                    content_type = self.curl.getinfo(CurlInfo.CONTENT_TYPE) or b""
                    declared = self.curl.getinfo(CurlInfo.CONTENT_LENGTH_DOWNLOAD_T)
                    check_type_and_length(
                        content_type.decode("latin-1") if isinstance(content_type, bytes) else content_type,
                        declared if isinstance(declared, int) and declared >= 0 else None,
                    )
            self.size += len(chunk)
            if self.size > MAX_CONTENT_BYTES:
                raise ContentTooLargeError("Page is too large.")
        except ScrapeError as e:
            self.error = e
            return CURL_WRITEFUNC_ERROR
        self.chunks.append(chunk)
        return len(chunk)

    def content(self) -> bytes:
        return b"".join(self.chunks)


def fetch_page(url: str, clearance: "Clearance | None" = None) -> Page:
    """Fetch over plain HTTP, following redirects by hand so every hop is checked.
    With `clearance`, the cookies and headers from a passed bot check are sent along."""
    deadline = time.monotonic() + FETCH_BUDGET_SECONDS
    session = http_session()
    # Cookies live only for this fetch: whatever its redirect chain sets is kept for the chain,
    # then wiped, so it is never sent on anyone else's request. The connections stay open.
    session.cookies.clear()
    headers = None
    if clearance is not None:
        headers = clearance.headers
        for cookie in clearance.cookies:
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path") or "/",
                secure=bool(cookie.get("secure")),
            )
    try:
        for _ in range(MAX_REDIRECTS + 1):
            validate_url(url)
            response, body = get_with_retries(session, url, deadline, headers)
            location = response.headers.get("location")
            if response.status_code in REDIRECT_CODES and location:
                url = urljoin(url, location)
                logger.info("Following redirect to %s", url)
                continue
            if response.status_code not in REDIRECT_CODES:
                check_headers(response)
            return Page(url, response.status_code, body)
        raise FetchError("Too many redirects")
    finally:
        session.cookies.clear()


def get_with_retries(
    session: requests.Session, url: str, deadline: float, headers: dict[str, str] | None = None
) -> tuple[requests.Response, bytes]:
    """GET the URL, retrying on transient failures as long as the next attempt still fits
    inside the fetch budget. Returns the response and its body."""
    for attempt in range(MAX_RETRIES + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("The site took too long to respond.")
        body = BodyReader(session.curl)
        response, error = None, None
        try:
            response = session.get(
                url, timeout=remaining, allow_redirects=False, content_callback=body, headers=headers
            )
        except RequestException as e:
            if body.error is not None:
                raise body.error from None
            error = e
        else:
            if response.status_code not in RETRY_STATUS_CODES:
                return response, body.content()
        wait = BACKOFF_SECONDS * 2**attempt
        # Stop when this was the last attempt, or when the wait plus a short request would overrun the budget.
        if attempt == MAX_RETRIES or time.monotonic() + wait + 1 > deadline:
            if response is not None:
                return response, body.content()
            if time.monotonic() >= deadline - 0.1:
                raise FetchError("The site took too long to send the page.") from error
            raise FetchError(f"Could not fetch the page: {error}") from error
        if response is not None:
            logger.info("Got %d from %s, retrying", response.status_code, url)
        else:
            logger.info("Attempt %d failed for %s: %s", attempt + 1, url, error)
        time.sleep(wait)
