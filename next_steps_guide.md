# Universal Scraper: Next Steps Guide

This guide walks you through improving the scraper one step at a time. Each step explains **what is wrong**, **why it matters**, and gives you **code to type in yourself**.

Work through the steps in order. Later steps build on earlier ones. Run the app after each step to check that it still works:

```bash
uvicorn main:app --reload
```

Then open `http://127.0.0.1:8000/docs` and try the `/scrape` endpoint.

> Written for the versions installed on your machine: Python 3.13, FastAPI 0.141, curl_cffi 0.16, BeautifulSoup 4.15.

---

## Table of contents

**Part 1: Project hygiene**
- [Step 1: Stop committing `__pycache__` and add a `.gitignore`](#step-1-stop-committing-__pycache__-and-add-a-gitignore)
- [Step 2: Add `requirements.txt`](#step-2-add-requirementstxt)
- [Step 3: Use `logging` instead of `print`](#step-3-use-logging-instead-of-print)

**Part 2: Critical fixes**
- [Step 4: Custom error classes](#step-4-custom-error-classes)
- [Step 5: Fix `normalize_url` (schemes, path encoding, credentials, international domains)](#step-5-fix-normalize_url)
- [Step 6: Block requests to internal addresses (SSRF)](#step-6-block-requests-to-internal-addresses-ssrf)
- [Step 7: Add a timeout, check every redirect, use a current browser fingerprint](#step-7-timeout-safe-redirects-current-fingerprint)

**Part 3: Error handling**
- [Step 8: Return proper HTTP errors from the endpoint](#step-8-return-proper-http-errors-from-the-endpoint)

**Part 4: Scraping quality**
- [Step 9: Check the content type and cap the response size](#step-9-check-the-content-type-and-cap-the-response-size)
- [Step 10: Rename `formatted` to `mode`, remove script and style tags, fix encoding, switch to lxml](#step-10-better-html-cleaning)
- [Step 11: Report 4xx/5xx status in the response, and add a Pydantic response model](#step-11-report-4xx5xx-and-add-a-response-model)
- [Checkpoint: what your files should look like now](#checkpoint-what-your-files-should-look-like-now)

**Part 5: Scaling**
- [Step 12: Reuse one HTTP session](#step-12-reuse-one-http-session)
- [Step 13: Retry with backoff](#step-13-retry-with-backoff)
- [Step 14: Rate limiting](#step-14-rate-limiting)
- [Step 15: Caching](#step-15-caching)
- [Step 16 (optional, advanced): Switch to async](#step-16-optional-advanced-switch-to-async)

**Part 6: Tests and docs**
- [Step 17: Unit tests with pytest](#step-17-unit-tests-with-pytest)
- [Step 18: Write a README](#step-18-write-a-readme)

---

# Part 1: Project hygiene

## Step 1: Stop committing `__pycache__` and add a `.gitignore`

**Problem:** `__pycache__/*.pyc` files are tracked in git. They are compiled bytecode that Python regenerates automatically. They make diffs noisy and should never be committed.

**1a.** Create a file called `.gitignore` in the project folder:

```gitignore
# Python
__pycache__/
*.py[cod]

# Virtual environments
.venv/
venv/

# Tooling
.pytest_cache/
.env
```

**1b.** Tell git to stop tracking the files it already has. They stay on your disk.

```bash
git rm -r --cached __pycache__
git add .gitignore
git commit -m "Ignore __pycache__ and add .gitignore"
```

---

## Step 2: Add `requirements.txt`

**Problem:** Nobody else (including you on a different machine) knows which packages the project needs.

Create `requirements.txt`:

```text
fastapi
uvicorn[standard]
curl_cffi
beautifulsoup4
lxml
```

Install everything (this also installs `lxml`, which you'll need in Step 10):

```bash
pip install -r requirements.txt
```

> Later, once everything works, you can pin exact versions with `pip freeze > requirements.txt` so installs are reproducible.

You'll add more packages to this file in later steps (`slowapi`, `cachetools`, `pytest`, `httpx`).

---

## Step 3: Use `logging` instead of `print`

**Problem:** `print("Error occured!", e)` has no timestamp, no severity level, and can't be filtered or sent to a file. (Also, "occured" is misspelled: it's "occurred".)

**3a.** At the top of `functions.py`, add:

```python
import logging

logger = logging.getLogger(__name__)
```

**3b.** At the top of `main.py`, after the imports, configure logging once for the whole app:

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
```

From now on, use:

```python
logger.info("Fetching %s", url)          # normal events
logger.warning("Blocked URL %s", url)    # suspicious but handled
logger.exception("Unexpected error")     # inside an except block; also logs the traceback
```

> Pass values as arguments (`"Fetching %s", url`) instead of f-strings. Logging only builds the string if the message is actually going to be shown.

---

# Part 2: Critical fixes

## Step 4: Custom error classes

**Problem:** Right now every function catches its own exceptions, prints them, and returns `None`. The caller can't tell *what* went wrong. We'll give each failure its own exception class, and each class knows which HTTP status code it maps to. Step 8 uses this.

Add this near the top of `functions.py` (below the imports):

```python
class ScrapeError(Exception):
    """Base class for all errors the scraper raises on purpose."""
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
```

---

## Step 5: Fix `normalize_url`

**Problems with the current version:**

1. **Any scheme is accepted.** `file:///etc/passwd`, `gopher://`, `dict://` all pass. `curl_cffi` uses libcurl, which supports those protocols. Only `http` and `https` should be allowed.
2. **`quote(path, safe="/%")` breaks valid URLs.** Characters like `: @ ! $ & ' ( ) * + , ; =` are legal in URL paths, but your code percent-encodes them. For example, `/wiki/Foo:Bar` becomes `/wiki/Foo%3ABar`, which some servers treat as a different page.
3. **`netloc.lower()` also lowercases credentials.** In `https://user:Password@host`, the password is case-sensitive.
4. **International domain names aren't handled.** `münchen.de` must be converted to its ASCII (IDNA/punycode) form `xn--mnchen-3ya.de`.
5. **`try/except` + `print` hides errors.** It's better to return `None` for a bad URL deliberately.

Replace the whole `normalize_url` function in `functions.py` with:

```python
ALLOWED_SCHEMES = {"http", "https"}

# Characters that are legal in a URL path (RFC 3986 "pchar" + "/").
# "%" is included so already-encoded sequences like %20 are not double-encoded.
PATH_SAFE_CHARS = "/%:@!$&'()*+,;=~"


def normalize_url(url: str) -> str | None:
    url = url.strip()

    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError for things like ":99999" or ":abc"
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return None

    host = parts.hostname  # already lowercase, without user:pass and port
    if not host:
        return None

    # IPv6 addresses need square brackets in a URL: http://[::1]:8000/
    if ":" in host:
        host = f"[{host}]"
    else:
        try:
            host = host.encode("idna").decode("ascii")  # münchen.de -> xn--mnchen-3ya.de
        except UnicodeError:
            return None

    # Keep credentials exactly as written (they are case-sensitive)
    userinfo = ""
    if "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0] + "@"

    netloc = userinfo + host
    if port is not None:
        netloc += f":{port}"

    path = quote(parts.path or "/", safe=PATH_SAFE_CHARS)

    # Drop the "#fragment". The browser uses it and it's never sent to the server.
    return urlunsplit((scheme, netloc, path, parts.query, ""))
```

**Try it** in a Python shell (`python` in the project folder):

```python
from functions import normalize_url
normalize_url("HTTPS://Example.COM/wiki/Foo:Bar#top")   # 'https://example.com/wiki/Foo:Bar'
normalize_url("file:///etc/passwd")                     # None
normalize_url("https://münchen.de")                     # 'https://xn--mnchen-3ya.de/'
normalize_url("http://[::1]:8000/x")                    # 'http://[::1]:8000/x'
normalize_url("http://example.com:abc")                 # None
```

> Python's built-in `"idna"` codec follows the older IDNA 2003 standard. For full IDNA 2008 support you can later `pip install idna` and use `idna.encode(host).decode()`.

---

## Step 6: Block requests to internal addresses (SSRF)

**Problem (the most important one):** Your server fetches **any** URL a user sends. That's called **SSRF (Server-Side Request Forgery)**. An attacker can make your server request:

- `http://localhost:8000/...`: your own internal services
- `http://192.168.1.1/`: your router or other machines on your network
- `http://169.254.169.254/latest/meta-data/`: if this ever runs on AWS, GCP or Azure, this address returns **cloud credentials**

Step 5 already blocks non-HTTP schemes. Now we also need to check **which IP address the hostname points to**, because `evil.com` could simply have a DNS record pointing to `127.0.0.1`.

**6a.** Add these imports at the top of `functions.py`:

```python
import ipaddress
import socket
```

**6b.** Add these functions:

```python
def is_public_host(hostname: str) -> bool:
    """True only if EVERY IP address the hostname resolves to is a public internet address."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False  # DNS lookup failed

    for family, type_, proto, canonname, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        # is_global is False for private, loopback, link-local and reserved ranges
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def validate_url(url: str) -> None:
    """Raise if the URL is not safe for us to fetch."""
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
        raise InvalidURLError("Only http and https URLs are allowed")
    if not is_public_host(parts.hostname):
        logger.warning("Blocked request to non-public host: %s", parts.hostname)
        raise BlockedURLError("This host is not allowed")
```

**Why check *every* IP?** A hostname can resolve to several addresses. If even one of them is private, the request might go there.

**Try it:**

```python
from functions import is_public_host
is_public_host("127.0.0.1")        # False
is_public_host("169.254.169.254")  # False
is_public_host("localhost")        # False
is_public_host("example.com")      # True
```

> **Known limitation, for later: DNS rebinding.** We resolve the hostname, and then curl resolves it *again* when fetching. A malicious DNS server can return a public IP the first time and `127.0.0.1` the second time. The full fix is to pin the IP you validated (libcurl's `CURLOPT_RESOLVE` option, available as `curl_options` in curl_cffi) or to route all traffic through a filtering proxy. What you have after this step already blocks the common attacks.

---

## Step 7: Timeout, safe redirects, current fingerprint

**Problems:**

1. **No timeout.** `requests.get(url, ...)` can hang **forever** on a slow or malicious server. FastAPI runs sync endpoints in a limited thread pool, so a handful of hanging requests can freeze the whole API.
2. **Redirects bypass the SSRF check.** `https://evil.com` can reply `302 Location: http://169.254.169.254/`, and curl follows it automatically. We need to follow redirects ourselves and validate every hop.
3. **`impersonate="chrome110"` is an old browser fingerprint.** Some anti-bot systems flag old browser versions. `impersonate="chrome"` always uses the newest Chrome your curl_cffi version supports.
4. **The function has no type hints.**

**7a.** Change the imports at the top of `functions.py`:

```python
from urllib.parse import urlsplit, urlunsplit, urljoin, quote

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
```

**7b.** Add these constants near the top:

```python
REQUEST_TIMEOUT = 10        # seconds
MAX_REDIRECTS = 5
REDIRECT_CODES = {301, 302, 303, 307, 308}
```

**7c.** Replace `fetch_page` with:

```python
def fetch_page(url: str) -> requests.Response:
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(url)  # validate every hop, not just the first URL

        try:
            response = requests.get(
                url,
                impersonate="chrome",
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except RequestException as e:
            logger.warning("Fetch failed for %s: %s", url, e)
            raise FetchError(f"Could not fetch the page: {e}") from e

        if response.status_code not in REDIRECT_CODES:
            return response

        location = response.headers.get("location")
        if not location:
            return response

        # Location can be relative ("/login"), so resolve it against the current URL
        url = urljoin(url, location)
        logger.info("Following redirect to %s", url)

    raise FetchError("Too many redirects")
```

> `raise ... from e` keeps the original exception attached, so the log shows the real root cause.

---

# Part 3: Error handling

## Step 8: Return proper HTTP errors from the endpoint

**Problems in `main.py`:**

1. `return "Invalid url"` sends the text `"Invalid url"` with status **200 OK**. The client thinks it succeeded.
2. On any exception, the function prints it and returns nothing. FastAPI then sends **200 OK** with body `null`, and the client has no idea what happened.
3. `if url == None:` should be `if url is None:`. `None` is a singleton, so you compare it by identity. Linters flag `== None`.

With the error classes from Step 4, the fix is short. Replace `main.py` with:

```python
import logging

from fastapi import FastAPI, HTTPException

from functions import (
    ScrapeError,
    normalize_url,
    fetch_page,
    clean_html_response,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Universal Scraper")


@app.get("/scrape")
def scrape_url(url: str, formatted: bool = False):
    normalized = normalize_url(url)
    if normalized is None:
        raise HTTPException(status_code=400, detail="Invalid URL. Only http/https URLs are supported.")

    try:
        response = fetch_page(normalized)
        content = clean_html_response(response, formatted)
    except ScrapeError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    return {
        "url": normalized,
        "status_code": response.status_code,
        "content": content,
    }
```

Notes:

- **There's no catch-all `except Exception` any more.** If something truly unexpected happens, FastAPI logs the full traceback and returns a `500 Internal Server Error`. That's the correct behaviour, and it means you'll *see* bugs instead of getting silent `null` responses.
- **Also remove the `try/except → print` from `clean_html_response`.** Step 10 rewrites that function anyway.
- `formatted` is still here for now. Step 10 replaces it.

**Test it:** in `/docs`, try `url=file:///etc/passwd` (400), `url=http://127.0.0.1` (403), and `url=https://example.com` (200).

---

# Part 4: Scraping quality

## Step 9: Check the content type and cap the response size

**Problem:** If someone passes a link to a PDF, an image, or a 500 MB video, your code downloads all of it, decodes it as text, and feeds it to BeautifulSoup. That's slow, memory-hungry, and returns garbage.

**9a.** Add a constant in `functions.py`:

```python
MAX_CONTENT_BYTES = 5 * 1024 * 1024  # 5 MB
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
```

**9b.** Add this function:

```python
def check_response(response: requests.Response) -> None:
    content_type = response.headers.get("content-type", "").lower()
    # Be lenient if the server sends no Content-Type at all
    if content_type and not content_type.startswith(HTML_CONTENT_TYPES):
        raise UnsupportedContentError(f"Not an HTML page (Content-Type: {content_type})")

    declared_length = response.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > MAX_CONTENT_BYTES:
        raise ContentTooLargeError("Page is too large")

    # Content-Length can be missing or wrong, so also check what actually arrived
    if len(response.content) > MAX_CONTENT_BYTES:
        raise ContentTooLargeError("Page is too large")
```

**9c.** In `fetch_page`, call it right before returning the final response:

```python
        if response.status_code not in REDIRECT_CODES:
            check_response(response)
            return response
```

> **Going further:** the checks above still download the body before rejecting it. For a hard limit, fetch with `stream=True`, read chunks with `response.iter_content()`, and stop when the running total goes over `MAX_CONTENT_BYTES`. Try that once everything else works.

---

## Step 10: Better HTML cleaning

**Problems in `clean_html_response`:**

1. **`formatted` does the opposite of what its name suggests.** `formatted=True` gives you plain text lines, and `formatted=False` gives you the whole HTML document. A clearer API is `mode="text"` or `mode="html"`.
2. **Wasted work.** `get_text()` and the line cleanup always run, even when you return HTML.
3. **JavaScript and CSS end up in the text.** `get_text()` includes everything inside `<script>` and `<style>` tags, so your "text" output contains minified JS.
4. **Encoding.** `response.text` decodes bytes using the HTTP header's charset guess. If you pass `response.content` (raw bytes), BeautifulSoup can also read `<meta charset="...">` inside the HTML, which is often more accurate.
5. **Speed.** The `lxml` parser is much faster than `html.parser` and handles broken HTML well. You installed it in Step 2.

**10a.** Add this import at the top of `functions.py`:

```python
from typing import Literal
```

**10b.** Replace `clean_html_response` with:

```python
NON_CONTENT_TAGS = ["script", "style", "noscript", "template"]


def clean_html_response(
    response: requests.Response,
    mode: Literal["text", "html"] = "text",
) -> str | list[str]:
    # Raw bytes let BeautifulSoup detect the encoding from <meta charset>
    soup = BeautifulSoup(response.content, "lxml")

    if mode == "html":
        return soup.prettify()

    for tag in soup(NON_CONTENT_TAGS):
        tag.decompose()  # remove the tag and everything inside it

    text = soup.get_text(separator="\n")
    return [line.strip() for line in text.splitlines() if line.strip()]
```

**10c.** Update `main.py`. Add the import:

```python
from typing import Literal
```

Then change the endpoint signature and the call:

```python
@app.get("/scrape")
def scrape_url(url: str, mode: Literal["text", "html"] = "text"):
    ...
        content = clean_html_response(response, mode)
```

Because `mode` is typed as a `Literal`, FastAPI **automatically** rejects `mode=banana` with a 422 error and shows a dropdown in `/docs`.

---

## Step 11: Report 4xx/5xx and add a response model

**Problem 1:** If the page returns `404 Not Found` or `500`, you still return its content as if it were a normal result. That can be fine (a 404 page still has content), but make it **visible** to the client with an `ok` field.

**Problem 2:** Without a response model, `/docs` doesn't describe what the endpoint returns, and nothing guarantees the response shape.

In `main.py`, add:

```python
from pydantic import BaseModel


class ScrapeResponse(BaseModel):
    url: str
    status_code: int
    ok: bool
    content: str | list[str]
```

Then update the decorator and the return statement:

```python
@app.get("/scrape", response_model=ScrapeResponse)
def scrape_url(url: str, mode: Literal["text", "html"] = "text"):
    ...
    return ScrapeResponse(
        url=normalized,
        status_code=response.status_code,
        ok=response.ok,          # True for 2xx/3xx, False for 4xx/5xx
        content=content,
    )
```

---

## Checkpoint: what your files should look like now

Compare your files with these. If something differs, find out why before moving on.

<details>
<summary><b>functions.py</b> (click to expand)</summary>

```python
import ipaddress
import logging
import socket
from typing import Literal
from urllib.parse import urlsplit, urlunsplit, urljoin, quote

from bs4 import BeautifulSoup
from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = {"http", "https"}
PATH_SAFE_CHARS = "/%:@!$&'()*+,;=~"
REQUEST_TIMEOUT = 10
MAX_REDIRECTS = 5
REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_CONTENT_BYTES = 5 * 1024 * 1024
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
NON_CONTENT_TAGS = ["script", "style", "noscript", "template"]


class ScrapeError(Exception):
    """Base class for all errors the scraper raises on purpose."""
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


def normalize_url(url: str) -> str | None:
    url = url.strip()

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return None

    host = parts.hostname
    if not host:
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
        raise InvalidURLError("Only http and https URLs are allowed")
    if not is_public_host(parts.hostname):
        logger.warning("Blocked request to non-public host: %s", parts.hostname)
        raise BlockedURLError("This host is not allowed")


def check_response(response: requests.Response) -> None:
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not content_type.startswith(HTML_CONTENT_TYPES):
        raise UnsupportedContentError(f"Not an HTML page (Content-Type: {content_type})")

    declared_length = response.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > MAX_CONTENT_BYTES:
        raise ContentTooLargeError("Page is too large")

    if len(response.content) > MAX_CONTENT_BYTES:
        raise ContentTooLargeError("Page is too large")


def fetch_page(url: str) -> requests.Response:
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(url)

        try:
            response = requests.get(
                url,
                impersonate="chrome",
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except RequestException as e:
            logger.warning("Fetch failed for %s: %s", url, e)
            raise FetchError(f"Could not fetch the page: {e}") from e

        if response.status_code not in REDIRECT_CODES:
            check_response(response)
            return response

        location = response.headers.get("location")
        if not location:
            return response

        url = urljoin(url, location)
        logger.info("Following redirect to %s", url)

    raise FetchError("Too many redirects")


def clean_html_response(
    response: requests.Response,
    mode: Literal["text", "html"] = "text",
) -> str | list[str]:
    soup = BeautifulSoup(response.content, "lxml")

    if mode == "html":
        return soup.prettify()

    for tag in soup(NON_CONTENT_TAGS):
        tag.decompose()

    text = soup.get_text(separator="\n")
    return [line.strip() for line in text.splitlines() if line.strip()]
```

</details>

<details>
<summary><b>main.py</b> (click to expand)</summary>

```python
import logging
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from functions import (
    ScrapeError,
    normalize_url,
    fetch_page,
    clean_html_response,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Universal Scraper")


class ScrapeResponse(BaseModel):
    url: str
    status_code: int
    ok: bool
    content: str | list[str]


@app.get("/scrape", response_model=ScrapeResponse)
def scrape_url(url: str, mode: Literal["text", "html"] = "text"):
    normalized = normalize_url(url)
    if normalized is None:
        raise HTTPException(status_code=400, detail="Invalid URL. Only http/https URLs are supported.")

    try:
        response = fetch_page(normalized)
        content = clean_html_response(response, mode)
    except ScrapeError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e

    return ScrapeResponse(
        url=normalized,
        status_code=response.status_code,
        ok=response.ok,
        content=content,
    )
```

</details>

Commit here. This is a solid, safe version.

```bash
git add .
git commit -m "Harden scraper: SSRF guard, timeouts, proper errors, cleaner text output"
```

---

# Part 5: Scaling

These steps matter once other people (or many requests) use the API.

## Step 12: Reuse one HTTP session

**Problem:** `requests.get(...)` creates a brand-new connection (DNS lookup, TCP handshake, TLS handshake) on every call. A `Session` keeps connections open and reuses them, which is much faster when you hit the same site repeatedly.

In `functions.py`, below the constants, create one session for the whole module:

```python
# curl_cffi's sync Session uses a separate curl handle per thread by default,
# so it's safe to share across FastAPI's worker threads.
session = requests.Session(impersonate="chrome")
```

Then in `fetch_page`, change `requests.get(` to `session.get(` and remove the `impersonate="chrome",` line, because the session already sets it:

```python
            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
```

---

## Step 13: Retry with backoff

**Problem:** Network hiccups happen. A request might time out once and succeed a second later. Servers also return `429 Too Many Requests` or `503 Service Unavailable` when they're temporarily overloaded. Retrying *with increasing waits* (backoff) handles this politely.

**13a.** Add to the imports in `functions.py`:

```python
import time
```

**13b.** Add constants:

```python
MAX_RETRIES = 3
RETRY_STATUS_CODES = {429, 502, 503, 504}
BACKOFF_SECONDS = 1  # waits 1s, 2s, 4s, ...
```

**13c.** Add a helper that does a single request with retries:

```python
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
```

**13d.** In `fetch_page`, replace the whole `try: ... except RequestException ...` block with one line:

```python
        response = get_with_retries(url)
```

> **Bonus:** servers often send a `Retry-After` header with `429`. Try reading it (`response.headers.get("retry-after")`) and sleeping for that many seconds (capped at a sensible maximum) instead of the default backoff.

---

## Step 14: Rate limiting

**Problem:** Without limits, anyone can use your server as a free, anonymous proxy, or flood it with requests.

**14a.** Add `slowapi` to `requirements.txt` and install it:

```bash
pip install slowapi
```

**14b.** In `main.py`, add the imports:

```python
from fastapi import FastAPI, HTTPException, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
```

**14c.** Right after `app = FastAPI(...)`, set up the limiter:

```python
limiter = Limiter(key_func=get_remote_address)  # limit per client IP
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
```

**14d.** Decorate the endpoint and add a `request: Request` parameter. **slowapi requires this parameter**, even though you don't use it yourself:

```python
@app.get("/scrape", response_model=ScrapeResponse)
@limiter.limit("10/minute")
def scrape_url(request: Request, url: str, mode: Literal["text", "html"] = "text"):
    ...
```

> Decorator order matters: `@app.get` must be on top and `@limiter.limit` below it.

Test it by calling the endpoint 11 times in a minute. The 11th call should return `429 Too Many Requests`.

---

## Step 15: Caching

**Problem:** If ten people scrape the same page within a minute, you fetch it ten times. A short-lived cache avoids repeated work and is kinder to the target site.

**15a.** Add `cachetools` to `requirements.txt` and install it:

```bash
pip install cachetools
```

**15b.** In `main.py`, add:

```python
import threading

from cachetools import TTLCache

# Up to 256 results, each kept for 5 minutes
cache: TTLCache = TTLCache(maxsize=256, ttl=300)
cache_lock = threading.Lock()  # sync endpoints run in multiple threads
```

**15c.** In the endpoint, check the cache after normalizing the URL and store the result before returning:

```python
    cache_key = (normalized, mode)
    with cache_lock:
        cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # ... fetch_page / clean_html_response as before ...

    result = ScrapeResponse(
        url=normalized,
        status_code=response.status_code,
        ok=response.ok,
        content=content,
    )
    with cache_lock:
        cache[cache_key] = result
    return result
```

> Think about it: should failed pages (`ok=False`) be cached? Maybe for a shorter time, or not at all. There's no single right answer.

---

## Step 16 (optional, advanced): Switch to async

**Why:** A sync endpoint occupies one thread for the whole time it's waiting on the network. FastAPI's thread pool is limited (40 threads by default). With `async`, a single thread can wait on hundreds of requests at once.

**Only do this after everything above works.** It touches almost every function.

What changes:

**16a.** Use `AsyncSession` and create it when the app starts (in `main.py`):

```python
from contextlib import asynccontextmanager

from curl_cffi.requests import AsyncSession


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = AsyncSession(impersonate="chrome")
    yield
    await app.state.http.close()


app = FastAPI(title="Universal Scraper", lifespan=lifespan)
```

**16b.** Make the fetching functions `async` and pass the session in (in `functions.py`):

```python
import asyncio


async def is_public_host(hostname: str) -> bool:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None)  # non-blocking DNS lookup
    except socket.gaierror:
        return False
    for family, type_, proto, canonname, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


async def validate_url(url: str) -> None:
    ...
    if not await is_public_host(parts.hostname):
        ...


async def fetch_page(session: AsyncSession, url: str) -> requests.Response:
    for _ in range(MAX_REDIRECTS + 1):
        await validate_url(url)
        response = await session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        ...
```

In the retry helper, replace `time.sleep(...)` with `await asyncio.sleep(...)`. **Never** call `time.sleep` inside async code, because it freezes the whole server.

**16c.** Make the endpoint `async`:

```python
@app.get("/scrape", response_model=ScrapeResponse)
@limiter.limit("10/minute")
async def scrape_url(request: Request, url: str, mode: Literal["text", "html"] = "text"):
    ...
    response = await fetch_page(request.app.state.http, normalized)
    # Parsing HTML is CPU work, so run it in a thread so it doesn't block other requests
    content = await asyncio.to_thread(clean_html_response, response, mode)
```

**16d.** Swap the `threading.Lock` around the cache for an `asyncio.Lock()`, or drop the lock. In async code only one coroutine runs at a time, and plain dict get/set don't `await`, so they can't be interrupted.

---

# Part 6: Tests and docs

## Step 17: Unit tests with pytest

**Why:** `normalize_url` and `clean_html_response` are *pure functions*: same input, same output, no network. They're the easiest code in the world to test, and tests let you refactor (like Step 16) without fear.

**17a.** Add `pytest` and `httpx` (needed by FastAPI's `TestClient`) to `requirements.txt` and install them:

```bash
pip install pytest httpx
```

**17b.** Create a `tests/` folder with an empty `tests/__init__.py`. Also create an empty `conftest.py` in the **project root**, which makes pytest able to import `main` and `functions`.

**17c.** Create `tests/test_functions.py`:

```python
import pytest

from functions import clean_html_response, is_public_host, normalize_url


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("HTTPS://Example.COM", "https://example.com/"),
        ("https://example.com/a b", "https://example.com/a%20b"),
        ("https://example.com/wiki/Foo:Bar", "https://example.com/wiki/Foo:Bar"),
        ("https://example.com/page#section", "https://example.com/page"),
        ("https://example.com/?q=1", "https://example.com/?q=1"),
        ("https://münchen.de", "https://xn--mnchen-3ya.de/"),
        ("http://[::1]:8000/x", "http://[::1]:8000/x"),
    ],
)
def test_normalize_url_valid(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "file:///etc/passwd",
        "ftp://example.com",
        "gopher://example.com",
        "example.com",           # no scheme
        "https://",              # no host
        "http://example.com:abc",  # bad port
    ],
)
def test_normalize_url_invalid(raw):
    assert normalize_url(raw) is None


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"],
)
def test_private_hosts_are_blocked(host):
    assert is_public_host(host) is False


class FakeResponse:
    """Stands in for a curl_cffi Response in tests."""

    def __init__(self, html: str, status_code: int = 200):
        self.content = html.encode("utf-8")
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers = {"content-type": "text/html; charset=utf-8"}


def test_clean_html_text_mode_strips_scripts_and_styles():
    html = """
        <html><head><style>body { color: red }</style></head>
        <body>
            <h1> Title </h1>
            <script>alert("hi")</script>
            <p>Hello</p>
        </body></html>
    """
    assert clean_html_response(FakeResponse(html), "text") == ["Title", "Hello"]


def test_clean_html_html_mode_returns_markup():
    result = clean_html_response(FakeResponse("<p>Hi</p>"), "html")
    assert "<p>" in result
```

**17d.** Create `tests/test_api.py`. It tests the endpoint **without** hitting the internet by replacing `fetch_page` with a fake:

```python
from fastapi.testclient import TestClient

import main
from tests.test_functions import FakeResponse

client = TestClient(main.app)


def test_invalid_scheme_returns_400():
    response = client.get("/scrape", params={"url": "file:///etc/passwd"})
    assert response.status_code == 400


def test_private_ip_returns_403():
    response = client.get("/scrape", params={"url": "http://127.0.0.1/"})
    assert response.status_code == 403


def test_successful_scrape(monkeypatch):
    # main.py did `from functions import fetch_page`, so we patch the name inside main
    monkeypatch.setattr(main, "fetch_page", lambda url: FakeResponse("<p>Hello</p>"))

    response = client.get("/scrape", params={"url": "https://example.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "https://example.com/"
    assert body["ok"] is True
    assert body["content"] == ["Hello"]


def test_invalid_mode_returns_422():
    response = client.get("/scrape", params={"url": "https://example.com", "mode": "banana"})
    assert response.status_code == 422
```

**17e.** Run the tests:

```bash
pytest -v
```

> If you added the cache (Step 15), clear it between tests (`main.cache.clear()`), or earlier tests may affect later ones. If you added rate limiting (Step 14), more than 10 test requests per minute will get a 429. Add `main.limiter.enabled = False` at the top of `test_api.py`. If you did Step 16 (async), the fake `fetch_page` must be an `async def` and take the session as its first argument.

---

## Step 18: Write a README

Create `README.md`. For a portfolio project it's the first thing people read. Include:

1. **What it is:** one or two sentences.
2. **Features:** SSRF protection, browser impersonation, text/HTML modes, rate limiting, and so on.
3. **Installation:**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate          # Windows
   pip install -r requirements.txt
   ```
4. **Running:** `uvicorn main:app --reload`
5. **Usage example:**
   ```bash
   curl "http://127.0.0.1:8000/scrape?url=https://example.com&mode=text"
   ```
   plus a sample JSON response.
6. **API parameters:** `url` and `mode`, with their allowed values.
7. **Error codes:** 400, 403, 413, 415, 422, 429, 502, and what each one means.
8. **Running tests:** `pytest -v`

---

# Summary checklist

Tick these off as you go:

- [ ] 1. `.gitignore` added, `__pycache__` untracked
- [ ] 2. `requirements.txt` added
- [ ] 3. `print` replaced with `logging`
- [ ] 4. Custom error classes
- [ ] 5. `normalize_url`: only http/https, correct path encoding, credentials preserved, IDNA
- [ ] 6. SSRF protection: private/loopback/link-local IPs blocked
- [ ] 7. Timeout added, redirects validated at every hop, `impersonate="chrome"`
- [ ] 8. Proper HTTP error codes, `is None` instead of `== None`, no silent `null` responses
- [ ] 9. Content-Type check and size limit
- [ ] 10. `formatted` replaced by `mode`, `<script>`/`<style>` removed, `response.content` + `lxml`
- [ ] 11. `ok` field for 4xx/5xx, Pydantic response model
- [ ] 12. One reusable HTTP session
- [ ] 13. Retries with backoff for timeouts and 429/5xx
- [ ] 14. Rate limiting with slowapi
- [ ] 15. TTL cache
- [ ] 16. (Optional) Async version
- [ ] 17. Tests with pytest
- [ ] 18. README
