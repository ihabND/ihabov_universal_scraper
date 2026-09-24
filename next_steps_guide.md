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
- [Step 15: Caching with Redis](#step-15-caching-with-redis)
- [Step 16: Use a proxy](#step-16-use-a-proxy)
- [Step 17: Load pages in a real browser with Playwright](#step-17-load-pages-in-a-real-browser-with-playwright)
- [Step 18: Fall back to the browser when the fast fetch fails](#step-18-fall-back-to-the-browser-when-the-fast-fetch-fails)
- [Step 19: Add a "Use JavaScript" checkbox](#step-19-add-a-use-javascript-checkbox)
- [Step 20: Deploy the browser to Fly.io](#step-20-deploy-the-browser-to-flyio)
- [Step 21 (optional, advanced): Switch to async](#step-21-optional-advanced-switch-to-async)

**Part 6: Tests and docs**
- [Step 22: Unit tests with pytest](#step-22-unit-tests-with-pytest)
- [Step 23: Write a README](#step-23-write-a-readme)

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

> Your code has changed since Part 4: `main.py` now has a `scrape()` helper that both the API (`/scrape`) and the website (`POST /`) use, plus API key checks from `auth.py`. The steps below build on that version.

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

**Problem:** Even with API keys, one key (or one person hammering the website form) can flood your server, run up your Fly bill, and get your server's IP banned by the sites you scrape.

**14a.** Add `slowapi` to `requirements.txt` and install it:

```bash
pip install slowapi
```

**14b.** In `main.py`, add the imports (`Request` is already imported):

```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
```

**14c.** On Fly.io, every request reaches your app through Fly's proxy. The address your app sees is the proxy's (like the `172.16.48.162` in your `fly logs`), so a plain per-IP limit would put **all users into one shared limit**. Fly sends the real address in the `Fly-Client-IP` header. Right after `app = FastAPI(...)`:

```python
def client_ip(request: Request) -> str:
    # Behind Fly's proxy the socket address is the proxy; the real one is in this header.
    return request.headers.get("fly-client-ip") or get_remote_address(request)


limiter = Limiter(key_func=client_ip)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
```

> Trusting a header is only safe because Fly's proxy overwrites it. When you run locally, there is no header and it falls back to the normal address.

**14d.** Decorate both scraping endpoints. The endpoint needs a `request: Request` parameter. **slowapi requires this parameter**, even though you don't use it yourself:

```python
@app.get("/scrape", response_model=ScrapeResponse,
         dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute")
def scrape_url(request: Request, url: str, mode: Literal["text", "html"] = "text"):
    ...


@app.post("/", response_class=HTMLResponse)
@limiter.limit("10/minute")
def index_submit(request: Request, ...):  # already has `request`
    ...
```

> Decorator order matters: `@app.get` / `@app.post` must be on top and `@limiter.limit` below it. Keep `dependencies=[Depends(require_api_key)]`, otherwise `/scrape` loses its API key check.

Test it by calling the endpoint 11 times in a minute. The 11th call should return `429 Too Many Requests`.

> For now, each Fly machine counts separately (you have two, so the real limit can be up to 20/minute). Step 15e fixes that with Redis.

---

## Step 15: Caching with Redis

**Problem:** If ten people scrape the same page within a minute, you fetch it ten times. A short-lived cache avoids repeated work and is kinder to the target site. Once the browser fallback exists (Step 18), a cache hit saves several seconds.

**Why Redis and not a Python dict:** a cache inside the Python process has two problems on Fly:

1. **You have two machines.** Each would have its own cache, so a page cached on machine A is still re-fetched when the next request lands on machine B.
2. **Machines auto-stop when idle.** Every stop wipes an in-memory cache, and yours stop after a few minutes without traffic.

Redis is a separate, very fast key–value server. Every machine talks to the same Redis, and it survives your app restarting. It also has built-in expiry: you store a value with "delete this after 300 seconds" and Redis handles the rest.

**15a.** Add `redis` to `requirements.txt` and install it:

```bash
pip install redis
```

**15b.** Run Redis locally. The easiest way on Windows is Docker Desktop:

```powershell
docker run -d --name scraper-redis -p 6379:6379 redis:7-alpine
$env:REDIS_URL = "redis://localhost:6379/0"
uvicorn main:app --reload
```

The container keeps running in the background. Start it again after a reboot with `docker start scraper-redis`.

> No Docker? The code below works without Redis too. If `REDIS_URL` isn't set, caching is simply turned off.

**15c.** Create a new file `cache.py`:

```python
import logging
import os

import redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300        # successful pages: 5 minutes
FAILED_CACHE_TTL_SECONDS = 30  # pages that returned 4xx/5xx: 30 seconds

REDIS_URL = os.environ.get("REDIS_URL")

# decode_responses=True gives back str instead of bytes.
# Short timeouts: if Redis is slow or down, skip the cache instead of hanging the request.
redis_client = (
    redis.Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_timeout=1,
        socket_connect_timeout=1,
    )
    if REDIS_URL
    else None
)
if redis_client is None:
    logger.warning("REDIS_URL is not set, caching is disabled.")


def cache_key(url: str, mode: str) -> str:
    return f"scrape:{mode}:{url}"


def get_cached(url: str, mode: str) -> str | None:
    if redis_client is None:
        return None
    try:
        return redis_client.get(cache_key(url, mode))
    except RedisError as e:
        logger.warning("Redis read failed: %s", e)
        return None


def set_cached(url: str, mode: str, value: str, ok: bool) -> None:
    if redis_client is None:
        return
    ttl = CACHE_TTL_SECONDS if ok else FAILED_CACHE_TTL_SECONDS
    try:
        redis_client.set(cache_key(url, mode), value, ex=ttl)
    except RedisError as e:
        logger.warning("Redis write failed: %s", e)
```

Things to notice:

- **Redis stores strings**, so the cache holds the response as JSON text. `cache.py` doesn't know about `ScrapeResponse`: it only moves strings in and out. That also avoids a circular import, since `main.py` imports `cache.py`.
- **Redis errors never break a scrape.** If Redis is down, `get_cached` returns `None` (a "miss") and `set_cached` does nothing. The page is just fetched normally. This is called *failing open*, and it's the right choice for a cache. It's the opposite of the API key check, which *fails closed*.
- **Failed pages are cached for only 30 seconds**, so a site that was briefly down isn't stuck as "down" for 5 minutes.
- **Exceptions are never cached.** If `fetch_page` raises, nothing reaches `set_cached`.

**15d.** In `main.py`, use the cache inside `scrape()`. Both the API and the website go through `scrape()`, so they share the cache:

```python
from cache import get_cached, set_cached


def scrape(url: str, mode: Literal["text", "html"]) -> ScrapeResponse:
    normalized = normalize_url(url)
    if normalized is None:
        raise InvalidURLError("Invalid URL, only HTTP and HTTPS.")

    cached = get_cached(normalized, mode)
    if cached is not None:
        logger.info("Cache hit for %s", normalized)
        return ScrapeResponse.model_validate_json(cached)

    response = fetch_page(normalized)
    content = clean_html_response(response, mode)
    result = ScrapeResponse(
        url=normalized,
        status_code=response.status_code,
        ok=response.ok,
        content=content,
    )
    set_cached(normalized, mode, result.model_dump_json(), result.ok)
    return result
```

Test it: scrape the same URL twice. The second call should be almost instant, and the log should say `Cache hit`. You can look inside Redis too:

```powershell
docker exec -it scraper-redis redis-cli keys "scrape:*"
docker exec -it scraper-redis redis-cli ttl "scrape:text:https://example.com/"
```

`ttl` shows how many seconds that entry has left.

**15e.** Share the rate limits from Step 14 through Redis too, so both Fly machines count together. Change the limiter line in `main.py`:

```python
import os

limiter = Limiter(
    key_func=client_ip,
    storage_uri=os.environ.get("REDIS_URL", "memory://"),
)
```

`memory://` is slowapi's default (per-process counting), used when there's no Redis.

**15f.** Create Redis on Fly.io. Fly offers managed Redis run by Upstash:

```powershell
fly redis create
```

Choose the same region as your app (`ams`). At the end it prints a URL like `redis://default:<password>@fly-<name>.upstash.io:6379`. It only works from inside your Fly apps, so it isn't exposed to the internet. Save it as a secret:

```powershell
fly secrets set REDIS_URL="redis://default:<password>@fly-<name>.upstash.io:6379" -a ihabov-universal-scraper
```

> Upstash is pay-per-use. Check the current price with `fly redis status <name>` or on Fly's pricing page. A cache this size costs very little.

---

## Step 16: Use a proxy

**Problem:** Every request your scraper sends comes from one IP address: your Fly machine's, in a data center in Amsterdam. Many sites block data-center IPs outright, show them CAPTCHAs, or rate-limit per IP. A proxy sends your requests out from a different IP. *Residential* proxies use home-internet IPs, which sites rarely block.

**16a.** Get a proxy URL from your provider. It looks like this:

```
http://username:password@proxy-host:port
```

It contains a password, so treat it like your API keys: **never put it in code or git.** Use an environment variable locally:

```powershell
$env:PROXY_URL = "http://username:password@proxy-host:port"
```

and a secret on Fly:

```powershell
fly secrets set PROXY_URL="http://username:password@proxy-host:port" -a ihabov-universal-scraper
```

**16b.** In `functions.py`, add `import os` to the imports, and below the constants:

```python
PROXY_URL = os.environ.get("PROXY_URL")

proxies = {
    "http": PROXY_URL,
    "https": PROXY_URL
} if PROXY_URL else None
```

The **keys** are the scheme of the *target* page: `"http"` is used for `http://` pages and `"https"` for `https://` pages. The **value** is the proxy's own address. One proxy normally handles both, which is why both keys point to the same URL. When `PROXY_URL` isn't set, `proxies` is `None` and requests go out directly, as before.

**16c.** Pass it to the session from Step 12:

```python
session = requests.Session(impersonate="chrome", proxies=proxies)
```

Every `session.get(...)`, including the retries from Step 13, now goes through the proxy.

**16d.** Check that it works. In a Python shell with `PROXY_URL` set:

```python
from functions import session
print(session.get("https://api.ipify.org").text)
```

It should print the **proxy's** IP, not yours. Run it once without `PROXY_URL` to compare.

> Why not test through `/scrape`? api.ipify.org returns plain text, and your content-type check from Step 9 rejects anything that isn't HTML with `415`.

Things to know:

- **If the proxy is down, requests fail** with a `RequestException`. Step 13 retries them, and then `fetch_page` raises `FetchError` (502). Once Step 18 is done, the browser gets a try too, through the same proxy.
- **Your SSRF check still works.** `validate_url` resolves the host itself before any request is sent.
- **Don't log `PROXY_URL`.** It contains the password. curl's error messages only include the proxy's host, which is fine to log.
- **Rotating IPs:** most providers give you a single "gateway" URL that picks a new IP on each request, so the code above is all you need. If you instead get a list of proxy URLs, pick one per request with `random.choice(...)` and pass `proxies=` to `session.get(...)`.
- **Residential proxies are usually billed per GB.** The Redis cache (Step 15) and blocking images in the browser (Step 17) both save you money.

---

## Step 17: Load pages in a real browser with Playwright

**Problem:** `curl_cffi` downloads the HTML and stops there. It doesn't run JavaScript. That fails in two common cases:

1. **JavaScript-rendered sites** (React, Vue, Angular, and so on). The downloaded HTML is an almost empty shell like `<div id="root"></div>`. The real content is added later by JavaScript.
2. **Bot checks that need JavaScript**, like Cloudflare's "Just a moment..." page. These usually come back as `403` or `503`.

Playwright controls a real Chromium browser. It runs the JavaScript and gives you the HTML *after* the page has been built. The catch: it's slow (2–5 seconds per page) and uses a lot of memory (roughly 150–300 MB per open browser). So it's used only as a fallback (Step 18) or when the user asks for it (Step 19).

**17a.** Add `playwright` to `requirements.txt`, install it, and download Chromium:

```bash
pip install playwright
playwright install chromium
```

**17b.** In `functions.py`, add the imports and constants. `unquote` joins the existing `urllib.parse` import:

```python
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit, urljoin, quote, unquote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BROWSER_TIMEOUT_MS = 20_000       # max time to load the page
NETWORK_IDLE_TIMEOUT_MS = 5_000   # extra time for JavaScript to fetch its data
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}  # not needed for text or HTML
MAX_BROWSERS = 2                  # browsers are memory-hungry, cap how many run at once

browser_slots = threading.BoundedSemaphore(MAX_BROWSERS)
```

A **semaphore** is a counter of free slots. `acquire()` takes a slot and waits if none are free; `release()` gives it back. It stops 20 simultaneous requests from starting 20 browsers and running your machine out of memory.

**17c.** Add a small class for the browser's result. `clean_html_response` only uses `.content`, and `scrape()` uses `.status_code` and `.ok`. So if the browser result has those three attributes, the rest of your code works with it unchanged. This is called *duck typing*:

```python
@dataclass
class BrowserResponse:
    """Has the same fields as a curl_cffi Response, so the rest of the code can use either."""
    status_code: int
    content: bytes

    @property
    def ok(self) -> bool:
        return self.status_code < 400
```

**17d.** Playwright takes the proxy in a different format from curl_cffi: the server, username, and password as separate fields. Convert `PROXY_URL` from Step 16:

```python
def playwright_proxy() -> dict | None:
    if not PROXY_URL:
        return None
    parts = urlsplit(PROXY_URL)
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    return proxy
```

> Chromium doesn't support SOCKS5 proxies with a username and password. If your provider offers both, use the `http://` URL.

**17e.** Protect against SSRF in the browser. This is **important**. Your Step 6 check covers the URL *you* request. A browser, though, fetches many things on its own: scripts, iframes, images, redirects. A malicious page could make it request `http://169.254.169.254/` or an internal address on Fly's private network. There are two defenses:

1. **Check every request the browser makes, and block the ones to non-public hosts.** Playwright's `page.route` lets you look at each request before it's sent. The same hook also blocks images, videos, and fonts, which saves memory, time, and proxy bandwidth.
2. **Check the redirects afterwards.** Playwright's documentation says: *"The handler will only be called for the first url if the response is a redirect."* So if a page redirects to an internal address, you can't stop that request. But you *can* refuse to return what came back. After loading, check the final URL and every redirect hop.

```python
def is_allowed_browser_request(url: str) -> bool:
    try:
        validate_url(url)
    except ScrapeError:
        return False
    return True


def handle_browser_route(route) -> None:
    request = route.request
    if (request.resource_type in BLOCKED_RESOURCE_TYPES
            or not is_allowed_browser_request(request.url)):
        route.abort()
    else:
        route.continue_()
```

**17f.** The function that loads a page in the browser:

```python
def fetch_with_browser(url: str) -> BrowserResponse:
    validate_url(url)
    if not browser_slots.acquire(timeout=BROWSER_TIMEOUT_MS / 1000):
        raise FetchError("All browsers are busy, try again later.")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(proxy=playwright_proxy())
            try:
                context = browser.new_context(service_workers="block")
                page = context.new_page()
                page.route("**/*", handle_browser_route)

                response = page.goto(url, wait_until="domcontentloaded",
                                     timeout=BROWSER_TIMEOUT_MS)
                if response is None:
                    raise FetchError("The browser got no response.")
                try:
                    # Give JavaScript a few seconds to fetch and draw the content.
                    page.wait_for_load_state("networkidle",
                                             timeout=NETWORK_IDLE_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    pass  # some pages never go idle (ads, live updates), use what we have

                html = page.content()
                status_code = response.status
                visited_urls = [page.url]
                request = response.request
                while request is not None:  # walk back through the redirects
                    visited_urls.append(request.url)
                    request = request.redirected_from
            finally:
                browser.close()
    except PlaywrightError as e:
        logger.warning("Browser failed for %s: %s", url, e)
        raise FetchError(f"The browser could not load the page: {e}") from e
    finally:
        browser_slots.release()

    for visited in visited_urls:
        validate_url(visited)  # raises BlockedURLError if a redirect went somewhere internal

    content = html.encode("utf-8")
    if len(content) > MAX_CONTENT_BYTES:
        raise ContentTooLargeError("Page is too large.")
    return BrowserResponse(status_code=status_code, content=content)
```

What each part does:

- **`browser_slots.acquire(...)` / `release()`:** waits up to 20 seconds for a free browser slot. `release()` is in a `finally` block, so the slot is given back even when something fails.
- **`with sync_playwright()` and `browser.close()`:** each call starts a fresh browser and always closes it. Starting a browser takes about 1 second. That's acceptable for a fallback, and it means no browser sits in memory between requests. (Step 21 shows how to keep one browser open instead.)
- **`service_workers="block"`:** service workers can make requests that `page.route` never sees, so they're turned off.
- **`wait_until="domcontentloaded"`, then `networkidle`:** the page's HTML has loaded, and then the browser waits up to 5 more seconds until the network goes quiet, meaning JavaScript has finished loading its data.
- **`PlaywrightTimeoutError` is a subclass of `PlaywrightError`.** A timeout in `goto` is therefore caught by the outer `except` and becomes a normal `FetchError` (502).
- **`page.content()`** returns the HTML *as it is now*, after JavaScript has run, not the original download.

**17g.** Try it in a Python shell. `quotes.toscrape.com/js/` is a practice site whose quotes are added by JavaScript:

```python
from functions import fetch_page, fetch_with_browser, clean_html_response

url = "https://quotes.toscrape.com/js/"
print(clean_html_response(fetch_page(url), "text")[:5])          # no quotes
print(clean_html_response(fetch_with_browser(url), "text")[:5])  # quotes!
```

---

## Step 18: Fall back to the browser when the fast fetch fails

**Problem:** You now have a fast fetcher (curl_cffi, well under a second) and a slow one that handles more sites (Playwright, 2–5 seconds). Try the fast one first, and use the browser only when the result looks wrong.

When does the result "look wrong"?

| Situation | Use the browser? | Why |
|---|---|---|
| `FetchError`: connection failed, timeout, too many redirects | Yes | TLS-level blocking sometimes only lets real browsers through |
| Status `403`, `429` or `503` | Yes | The usual codes for bot checks (after Step 13's retries) |
| Status `200` but almost no text | Yes | Probably a JavaScript-rendered page |
| Other `4xx`/`5xx` (like `404`) | No | The page really is missing or broken |
| `InvalidURLError`, `BlockedURLError`, `ContentTooLargeError`, `UnsupportedContentError` | No | A browser gives the same answer, and blocked URLs must stay blocked |

**18a.** In `functions.py`:

```python
BROWSER_FALLBACK_STATUS_CODES = {403, 429, 503}
MIN_TEXT_CHARS = 100


def needs_browser(response: requests.Response) -> bool:
    if response.status_code in BROWSER_FALLBACK_STATUS_CODES:
        return True
    if not response.ok:
        return False
    text = " ".join(clean_html_response(response, "text"))
    return len(text) < MIN_TEXT_CHARS


def fetch_page_with_fallback(url: str) -> requests.Response | BrowserResponse:
    try:
        response = fetch_page(url)
    except FetchError as e:
        logger.info("Fast fetch failed for %s (%s), trying the browser", url, e)
        return fetch_with_browser(url)

    if not needs_browser(response):
        return response

    logger.info("Got %d or too little text from %s, trying the browser",
                response.status_code, url)
    try:
        return fetch_with_browser(url)
    except ScrapeError as e:
        logger.warning("Browser fallback failed for %s: %s", url, e)
        return response  # the fast result is better than an error
```

Things to notice:

- **Only `FetchError` is caught** in the first `try`. Every other `ScrapeError` (blocked URL, not HTML, too large) passes straight up to the user, exactly as before.
- **If the browser also fails, return the original response.** A `403` page is still a real answer, and `ok=False` tells the user something went wrong.
- **Why 100 characters?** example.com has about 140 characters of text, and a JavaScript shell like "You need to enable JavaScript to run this app." has about 45. Tune the number if you see wrong fallbacks in your logs.
- `needs_browser` parses the HTML once, and `scrape()` parses it again later. That's a few milliseconds, which is tiny next to a network request.

**18b.** Tell the user which fetcher was used. Add a field to `ScrapeResponse` in `main.py`:

```python
class ScrapeResponse(BaseModel):
    url: str
    status_code: int
    ok: bool
    fetched_with: Literal["http", "browser"] = "http"
    content: str | list[str]
```

The default `"http"` matters: results cached in Redis before this change don't have the field, and without a default they would fail to load.

**18c.** In `main.py`, import `BrowserResponse` and `fetch_page_with_fallback` from `functions` (you can drop `fetch_page` from that import), and update `scrape()`:

```python
    response = fetch_page_with_fallback(normalized)
    content = clean_html_response(response, mode)
    result = ScrapeResponse(
        url=normalized,
        status_code=response.status_code,
        ok=response.ok,
        fetched_with="browser" if isinstance(response, BrowserResponse) else "http",
        content=content,
    )
```

Test it:

- `https://example.com` should return `"fetched_with": "http"`.
- `https://quotes.toscrape.com/js/` should return `"fetched_with": "browser"`, with the quotes in `content`.

> Browser scrapes can take 20+ seconds in the worst case. Raise the `timeout=` in your client scripts to 60.

---

## Step 19: Add a "Use JavaScript" checkbox

**Problem:** The automatic fallback only kicks in when the fast result *looks* wrong. Sometimes it looks fine but is incomplete: the page has plenty of text, yet the part you want (prices, comments, search results) is loaded by JavaScript. Let the user say "always use the browser for this one".

**19a.** In `functions.py`, add a `use_js` parameter that skips the fast fetch:

```python
def fetch_page_with_fallback(
    url: str,
    use_js: bool = False,
) -> requests.Response | BrowserResponse:
    if use_js:
        return fetch_with_browser(url)
    try:
        response = fetch_page(url)
    ...  # rest unchanged
```

**19b.** In `cache.py`, make `use_js` part of the cache key. Otherwise a request *with* JavaScript could get back a cached result from *without* it:

```python
def cache_key(url: str, mode: str, use_js: bool) -> str:
    js = "js" if use_js else "nojs"
    return f"scrape:{mode}:{js}:{url}"
```

Add a `use_js` parameter to `get_cached` and `set_cached` too, and pass it on to `cache_key`:

```python
def get_cached(url: str, mode: str, use_js: bool) -> str | None:
    ...
        return redis_client.get(cache_key(url, mode, use_js))
    ...


def set_cached(url: str, mode: str, use_js: bool, value: str, ok: bool) -> None:
    ...
        redis_client.set(cache_key(url, mode, use_js), value, ex=ttl)
    ...
```

**19c.** In `main.py`, pass it through `scrape()` and both endpoints:

```python
def scrape(url: str, mode: Literal["text", "html"], use_js: bool = False) -> ScrapeResponse:
    ...
    cached = get_cached(normalized, mode, use_js)
    ...
    response = fetch_page_with_fallback(normalized, use_js)
    ...
    set_cached(normalized, mode, use_js, result.model_dump_json(), result.ok)
    return result


@app.get("/scrape", response_model=ScrapeResponse,
         dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute")
def scrape_url(request: Request, url: str,
               mode: Literal["text", "html"] = "text", js: bool = False):
    try:
        return scrape(url, mode, js)
    except ScrapeError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e


@app.post("/", response_class=HTMLResponse)
@limiter.limit("10/minute")
def index_submit(
    request: Request,
    url: str = Form(...),
    mode: Literal["text", "html"] = Form("text"),
    api_key: str = Form(""),
    use_js: bool = Form(False),
):
    context = {"url": url, "mode": mode, "api_key": api_key, "use_js": use_js}
    ...
            context["result"] = scrape(url, mode, use_js)
    ...
```

API users turn it on with `&js=true`. FastAPI turns `true`, `1`, `yes` and `on` into `True`.

**19d.** Add the checkbox to `templates/index.html`, between the mode select and the API key field:

```html
    <label class="checkbox">
      <input type="checkbox" name="use_js" value="true" {% if use_js %}checked{% endif %}>
      Use JavaScript (slower, for pages that load their content with JavaScript)
    </label>
```

How HTML checkboxes are sent:

- **When ticked**, the form sends `use_js=true`.
- **When unticked**, the form sends **nothing at all** for that field. That's why the endpoint has a default of `Form(False)`.
- **`{% if use_js %}checked{% endif %}`** keeps the box ticked after the page reloads with the result.

The existing `label` style stacks things vertically. Add this to the `<style>` block so the box sits next to its text:

```css
    .checkbox { display: flex; align-items: center; gap: .5rem; font-weight: normal; }
```

While you're in the template, show which fetcher was used next to the status:

```html
    <p class="meta">{{ result.url }} &middot; status {{ result.status_code }} &middot; via {{ result.fetched_with }}</p>
```

> **Think about it:** a browser scrape costs about 20× more than a normal one. Should `js=true` requests get a stricter rate limit (for example `3/minute`)? slowapi can't pick a limit based on a parameter directly. One option is a separate endpoint with its own `@limiter.limit`.

---

## Step 20: Deploy the browser to Fly.io

The browser needs three things on Fly that your app doesn't have yet: Chromium installed in the image, more memory, and the new secrets.

**20a.** Install Chromium in the `Dockerfile`. Change the second stage:

```dockerfile
FROM python:3.12.14-slim
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
COPY --from=builder /app/.venv .venv/
# Downloads Chromium plus the Linux libraries it needs (fonts, graphics, ...)
RUN .venv/bin/playwright install --with-deps chromium
COPY . .
CMD ["/app/.venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

- **`PLAYWRIGHT_BROWSERS_PATH`** puts Chromium in a fixed folder, and Playwright looks there at runtime too.
- **The install runs *before* `COPY . .`.** Docker caches each step. Code changes only affect `COPY . .`, so Chromium (hundreds of MB) isn't downloaded again on every deploy.
- The image grows by about 400–600 MB, so the first deploy is slower, and so is a cold start.

> If `--with-deps` fails because the Debian version isn't supported, use Playwright's ready-made image as the base of the second stage instead: `FROM mcr.microsoft.com/playwright/python:v<version>-noble`, with the same version as the `playwright` package in your requirements.

**20b.** Give the machines more memory. In `fly.toml`:

```toml
[[vm]]
  cpu_kind = 'shared'
  cpus = 1
  memory_mb = 1024
```

With 256 MB, Chromium gets killed as soon as it starts. `fly logs` would show `Out of memory: Killed process`. 1 GB fits `MAX_BROWSERS = 2`. If you still see out-of-memory errors, lower it to 1. More memory costs more per hour (see Fly's pricing page), but with auto-stop you only pay while a machine is running.

**20c.** Check that the secrets are there, then deploy:

```powershell
fly secrets list -a ihabov-universal-scraper   # API_KEYS, REDIS_URL, PROXY_URL
git push                                        # or: fly deploy
fly logs -a ihabov-universal-scraper
```

Then scrape `https://quotes.toscrape.com/js/`, once with the checkbox and once without. Both should return quotes: the first straight from the browser, the second through the automatic fallback.

---

## Step 21 (optional, advanced): Switch to async

**Why:** A sync endpoint occupies one thread for the whole time it's waiting on the network. FastAPI's thread pool is limited (40 threads by default). With `async`, a single thread can wait on hundreds of requests at once.

**Only do this after everything above works.** It touches almost every function.

What changes:

**21a.** Use `AsyncSession` and create it when the app starts (in `main.py`). Keep the proxy from Step 16:

```python
from contextlib import asynccontextmanager

from curl_cffi.requests import AsyncSession

from functions import proxies


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = AsyncSession(impersonate="chrome", proxies=proxies)
    yield
    await app.state.http.close()


app = FastAPI(
    title="Ihabov Universal Scraper",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
```

**21b.** Make the fetching functions `async` and pass the session in (in `functions.py`):

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

**21c.** Make the endpoints and `scrape()` `async`:

```python
@app.get("/scrape", response_model=ScrapeResponse,
         dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute")
async def scrape_url(request: Request, url: str,
                     mode: Literal["text", "html"] = "text", js: bool = False):
    ...
    response = await fetch_page_with_fallback(request.app.state.http, normalized, js)
    # Parsing HTML is CPU work, so run it in a thread so it doesn't block other requests
    content = await asyncio.to_thread(clean_html_response, response, mode)
```

**21d.** Redis: switch to the async client. It has the same methods, but you `await` them:

```python
from redis import asyncio as aioredis

redis_client = aioredis.Redis.from_url(REDIS_URL, decode_responses=True,
                                       socket_timeout=1, socket_connect_timeout=1)

async def get_cached(...):
    ...
    return await redis_client.get(cache_key(url, mode, use_js))
```

**21e.** Playwright: the sync API **refuses to run inside async code**, so switch to `playwright.async_api`. Async also unlocks a big speed-up: start **one** browser when the app starts (in `lifespan`) and keep it open. Each request then only opens a new *context* (like a fresh private window), which takes milliseconds instead of about a second:

```python
from playwright.async_api import async_playwright


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = AsyncSession(impersonate="chrome", proxies=proxies)
    app.state.playwright = await async_playwright().start()
    app.state.browser = await app.state.playwright.chromium.launch(proxy=playwright_proxy())
    yield
    await app.state.browser.close()
    await app.state.playwright.stop()
    await app.state.http.close()
```

In `fetch_with_browser`, use `context = await browser.new_context(service_workers="block")` and `await context.close()` in the `finally`, and put `await` in front of every page call. Replace the `threading.BoundedSemaphore` with an `asyncio.Semaphore(MAX_BROWSERS)` used as `async with browser_slots:`.

> Downside: the browser now stays in memory the whole time the machine runs, even when nobody uses it. With 1 GB that's fine.

---

# Part 6: Tests and docs

## Step 22: Unit tests with pytest

**Why:** `normalize_url`, `clean_html_response` and `needs_browser` are *pure functions*: same input, same output, no network. They're the easiest code in the world to test, and tests let you refactor (like Step 21) without fear.

**22a.** Add `pytest` and `httpx` (needed by FastAPI's `TestClient`) to `requirements.txt` and install them:

```bash
pip install pytest httpx
```

**22b.** Create a `tests/` folder with an empty `tests/__init__.py`. Also create `conftest.py` in the **project root**. Its presence lets pytest import `main` and `functions`, and pytest loads it *before* any test file. That makes it the place to set up the environment, because `auth.py`, `cache.py` and `functions.py` read their environment variables at import time:

```python
import os

os.environ["API_KEYS"] = "test-key"
os.environ.pop("REDIS_URL", None)  # no cache in tests, every test starts clean
os.environ.pop("PROXY_URL", None)  # tests must not use (and pay for) the proxy
```

**22c.** Create `tests/test_functions.py`:

```python
import pytest

from functions import clean_html_response, is_public_host, needs_browser, normalize_url


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


def test_needs_browser():
    long_text = "<p>" + "word " * 50 + "</p>"
    assert needs_browser(FakeResponse(long_text, status_code=403))    # bot check
    assert needs_browser(FakeResponse('<div id="root"></div>'))         # JS shell
    assert not needs_browser(FakeResponse(long_text))                   # normal page
    assert not needs_browser(FakeResponse("<p>Not found</p>", 404))     # really missing
```

**22d.** Create `tests/test_api.py`. It tests the endpoint **without** hitting the internet (or starting a browser) by replacing `fetch_page_with_fallback` with a fake:

```python
from fastapi.testclient import TestClient

import main
from tests.test_functions import FakeResponse

main.limiter.enabled = False  # tests send more than 10 requests per minute
client = TestClient(main.app)
HEADERS = {"X-API-Key": "test-key"}  # set in conftest.py


def test_missing_api_key_returns_401():
    response = client.get("/scrape", params={"url": "https://example.com"})
    assert response.status_code == 401


def test_invalid_scheme_returns_400():
    response = client.get("/scrape", params={"url": "file:///etc/passwd"}, headers=HEADERS)
    assert response.status_code == 400


def test_private_ip_returns_403():
    response = client.get("/scrape", params={"url": "http://127.0.0.1/"}, headers=HEADERS)
    assert response.status_code == 403


def test_successful_scrape(monkeypatch):
    # main.py did `from functions import fetch_page_with_fallback`,
    # so we patch the name inside main
    monkeypatch.setattr(main, "fetch_page_with_fallback",
                        lambda url, use_js=False: FakeResponse("<p>Hello</p>"))

    response = client.get("/scrape", params={"url": "https://example.com"}, headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "https://example.com/"
    assert body["ok"] is True
    assert body["fetched_with"] == "http"
    assert body["content"] == ["Hello"]


def test_js_flag_is_passed_through(monkeypatch):
    calls = []

    def fake_fetch(url, use_js=False):
        calls.append(use_js)
        return FakeResponse("<p>Hello</p>")

    monkeypatch.setattr(main, "fetch_page_with_fallback", fake_fetch)
    client.get("/scrape", params={"url": "https://example.com", "js": "true"}, headers=HEADERS)
    assert calls == [True]


def test_invalid_mode_returns_422():
    response = client.get("/scrape", params={"url": "https://example.com", "mode": "banana"},
                          headers=HEADERS)
    assert response.status_code == 422
```

**22e.** Run the tests:

```bash
pytest -v
```

> If you did Step 21 (async), the fake `fetch_page_with_fallback` must be an `async def` and take the session as its first argument.

---

## Step 23: Write a README

Create `README.md`. For a portfolio project it's the first thing people read. Include:

1. **What it is:** one or two sentences.
2. **Features:** API keys, SSRF protection, browser impersonation, text/HTML modes, rate limiting, Redis cache, proxy support, and the Playwright fallback with the "Use JavaScript" option.
3. **Installation:**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate          # Windows
   pip install -r requirements.txt
   playwright install chromium
   ```
4. **Running:** the environment variables (`API_KEYS` required; `REDIS_URL` and `PROXY_URL` optional), then `uvicorn main:app --reload`.
5. **Usage example:**
   ```bash
   curl -H "X-API-Key: YOUR_KEY" "http://127.0.0.1:8000/scrape?url=https://example.com&mode=text&js=false"
   ```
   plus a sample JSON response.
6. **API parameters:** `url`, `mode` and `js`, with their allowed values.
7. **Error codes:** 400, 401, 403, 413, 415, 422, 429, 502, and what each one means.
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
- [ ] 14. Rate limiting with slowapi, using the real client IP behind Fly
- [ ] 15. Redis cache (locally with Docker, on Fly with Upstash), shared rate limits
- [ ] 16. Proxy from `PROXY_URL`
- [ ] 17. Playwright browser fetcher with SSRF checks and a browser limit
- [ ] 18. Automatic browser fallback, `fetched_with` in the response
- [ ] 19. "Use JavaScript" checkbox and `js=true` API parameter
- [ ] 20. Chromium in the Dockerfile, 1 GB memory on Fly
- [ ] 21. (Optional) Async version
- [ ] 22. Tests with pytest
- [ ] 23. README
