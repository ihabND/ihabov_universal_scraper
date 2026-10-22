"""The web app: the JSON API, the web pages, accounts, and the health check."""

import hashlib
import json
import logging
import math
import re
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request, Response, Security
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import auth, cache, users
from .auth import MAX_KEYS_PER_USER, TooManyKeys, api_key_header
from .browser import BROWSER_ENABLED
from .errors import ScrapeError
from .parsers import PARSERS
from .quota import IP_LIMITS, USER_LIMITS, describe, take_ip, take_login, take_signup, take_user, user_usage
from .schemas import BatchRequest, BatchResponse, BrowserOption, Mode, ScrapeResponse
from .service import run_batch, scrape
from .users import SESSION_COOKIE, SESSION_SECONDS

PACKAGE_DIR = Path(__file__).parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Ihabov Universal Scraper",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

_asset_hashes: dict[str, tuple[float, str]] = {}


def asset_url(name: str) -> str:
    """URL of a static file with a fingerprint of its contents, like /static/app.css?v=3f9a1c2b7d.
    Any change to the file changes the URL, so browsers load the new version instead of a cached one.
    The fingerprint is recomputed only when the file's modification time changes."""
    path = PACKAGE_DIR / "static" / name
    mtime = path.stat().st_mtime
    cached = _asset_hashes.get(name)
    if cached is None or cached[0] != mtime:
        cached = (mtime, hashlib.sha256(path.read_bytes()).hexdigest()[:10])
        _asset_hashes[name] = cached
    return f"/static/{name}?v={cached[1]}"


def format_time(timestamp: float | None) -> str:
    if timestamp is None:
        return "Never"
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC")


templates.env.globals["asset_url"] = asset_url
templates.env.globals["max_keys"] = MAX_KEYS_PER_USER
templates.env.globals["user_limit"] = describe(USER_LIMITS)  # "10 requests a minute"
templates.env.filters["time"] = format_time


class RateLimited(Exception):
    def __init__(self, limit, reset_at: float):
        self.limit, self.reset_at = limit, reset_at


@app.exception_handler(RateLimited)
def rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
    # Same body as before (slowapi's format), plus a Retry-After header.
    return JSONResponse(
        {"error": f"Rate limit exceeded: {exc.limit}"},
        status_code=429,
        headers={"Retry-After": str(seconds_until(exc.reset_at))},
    )


class LoginRequired(Exception):
    def __init__(self, next_page: str):
        self.next_page = next_page


@app.exception_handler(LoginRequired)
def login_required(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse(f"/login?next={quote(exc.next_page)}", status_code=303)


def seconds_until(moment: float) -> int:
    return max(1, math.ceil(moment - time.time()))


def client_ip(request: Request) -> str:
    return request.headers.get("fly-client-ip") or (request.client.host if request.client else "unknown")


# ---- Sessions and forms ------------------------------------------------------------------------


def current_session(request: Request) -> users.Session | None:
    """The logged-in session, looked up once per request."""
    if not hasattr(request.state, "session"):
        request.state.session = users.find_session(request.cookies.get(SESSION_COOKIE))
    return request.state.session


def tokens_match(sent: str | None, expected: str) -> bool:
    return bool(sent) and secrets.compare_digest(sent.encode(), expected.encode())


def same_origin(request: Request) -> bool:
    """False when a browser says the request comes from another site. Browsers send Origin on every
    form POST; a request without it isn't from a browser form on another site."""
    origin = request.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == request.headers.get("host")


def form_session(request: Request, csrf_token: str, page: str) -> users.Session:
    """For form posts that need a login: the session, after checking that the form came from this
    site. Without a login, the user is sent to log in and then back to `page`."""
    session = current_session(request)
    if session is None:
        raise LoginRequired(page)
    if not same_origin(request) or not tokens_match(csrf_token, session.csrf_token):
        raise HTTPException(status_code=403, detail="This form has expired. Go back, reload the page and try again.")
    return session


def page_session(request: Request, page: str) -> users.Session:
    session = current_session(request)
    if session is None:
        raise LoginRequired(page)
    return session


def safe_next(next_page: str) -> str:
    """Where to go after logging in. Only paths on this site, so a link can't send the user elsewhere."""
    if next_page.startswith("/") and not next_page.startswith("//") and "\\" not in next_page:
        return next_page
    return "/app"


def is_https(request: Request) -> bool:
    # Fly's proxy ends TLS and says so in X-Forwarded-Proto.
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def logged_in_redirect(request: Request, token: str, to: str) -> RedirectResponse:
    response = RedirectResponse(to, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=is_https(request),
        samesite="lax",
    )
    return response


def logged_out_redirect(to: str = "/") -> RedirectResponse:
    response = RedirectResponse(to, status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


def render(request: Request, template: str, context: dict | None = None, status_code: int = 200) -> HTMLResponse:
    """A page, with the logged-in user (or None) and the CSRF token for its forms."""
    session = current_session(request)
    base = {"user": session.user if session else None, "csrf_token": session.csrf_token if session else ""}
    return templates.TemplateResponse(request, template, base | (context or {}), status_code=status_code)


# ---- Authorization -----------------------------------------------------------------------------


def authorize(request: Request, api_key: str | None, cost: int = 1, csrf_token: str | None = None) -> int:
    """Find the user behind a scrape request and charge their quota. Returns the user id.

    API clients send a key in X-API-Key. The website sends its login cookie plus the session's CSRF
    token (in X-CSRF-Token, or a form field), so another site can't spend a user's quota through
    their cookie. A missing or wrong key or login is counted against the caller's IP (10 a minute
    by default), so guessing keys stays slow. A user is charged per URL, all their keys together."""
    key = None
    user_id = None
    if api_key:
        key = auth.find_key(api_key)
        user_id = key.user_id if key else None
    else:
        session = current_session(request)
        sent = csrf_token if csrf_token is not None else request.headers.get("x-csrf-token")
        if session and tokens_match(sent, session.csrf_token):
            user_id = session.user.id
    if user_id is None:
        exceeded = take_ip(client_ip(request))
        if exceeded:
            raise RateLimited(*exceeded)
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    exceeded = take_user(user_id, cost)
    if exceeded:
        raise RateLimited(*exceeded)
    users.record_usage(user_id, key.id if key else None, cost)
    return user_id


def owner_of(user_id: int) -> str:
    """Who is asking, for the browser's saved bot-check cookies, which are kept per user."""
    return f"user:{user_id}"


def add_rate_headers(response: Response, user_id: int) -> None:
    usage = user_usage(user_id)
    response.headers["X-RateLimit-Limit"] = str(usage.limit.amount)
    response.headers["X-RateLimit-Remaining"] = str(usage.remaining)
    response.headers["X-RateLimit-Reset"] = str(seconds_until(usage.reset_at))


@app.get("/health")
def health() -> dict:
    """For uptime monitors and the hosting platform. Needs no key and never scrapes anything.
    The app works without Redis (just without the cache), so a Redis problem is reported, not fatal."""
    return {"status": "ok", "redis": cache.redis_status(), "browser": "on" if BROWSER_ENABLED else "off"}


# ---- API -------------------------------------------------------------------------------------


@app.get("/scrape", response_model=ScrapeResponse)
def scrape_url(
    request: Request,
    response: Response,
    url: str,
    mode: Mode = "html",
    browser: BrowserOption = "auto",
    api_key: str | None = Security(api_key_header),
):
    # The key is checked inside the endpoint, before anything is fetched, so that wrong keys count
    # toward the IP limit too.
    user_id = authorize(request, api_key)
    add_rate_headers(response, user_id)
    try:
        return scrape(url, mode, browser, owner=owner_of(user_id))
    except ScrapeError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e


@app.post("/scrape/batch", response_model=BatchResponse)
def scrape_batch(
    request: Request, response: Response, body: BatchRequest, api_key: str | None = Security(api_key_header)
):
    """Scrape up to 50 URLs in one call. Every URL is handled exactly as GET /scrape would handle
    it; failures are reported per URL instead of failing the whole batch."""
    user_id = authorize(request, api_key, cost=len(body.urls))
    add_rate_headers(response, user_id)
    return run_batch(body.urls, body.mode, body.browser, owner_of(user_id))


# ---- Web pages -----------------------------------------------------------------------------

SITE_COLORS = ["#3b6fd6", "#2f8a4c", "#b5730b", "#7a4bd0", "#c8372d", "#1b8a8a", "#d0631d", "#a67c00"]


def site_cards() -> list[dict]:
    """One card per supported site for the front page. A site with several parsers
    (books.toscrape.com has a listing and a book page) gets one card that opens its first example."""
    by_host: dict[str, dict] = {}
    for parser in PARSERS:
        card = by_host.setdefault(
            parser.host, {"host": parser.host, "example": parser.example, "labels": [], "shapes": []}
        )
        card["labels"].append(parser.label)
        card["shapes"].append(parser.shape)
    return [
        {
            "host": card["host"],
            "example": card["example"],
            "label": " or ".join(card["labels"][:1] + [label.lower() for label in card["labels"][1:]]),
            "shape": " / ".join(dict.fromkeys(card["shapes"])),
            "color": SITE_COLORS[i % len(SITE_COLORS)],
        }
        for i, card in enumerate(by_host.values())
    ]


SITE_CARDS = site_cards()

# Real, shortened results for each site's example page, shown on the front page. Refreshed with
# scripts/refresh_examples.py.
EXAMPLES: dict[str, dict] = json.loads((PACKAGE_DIR / "examples.json").read_text(encoding="utf-8"))

JSON_TOKEN = re.compile(r'("(?:\\.|[^"\\])*")(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false)\b|\b(null)\b')


def highlight_json(value) -> Markup:
    """Indented JSON with keys, strings, numbers, booleans and null wrapped in spans for color.
    Every piece of text is HTML-escaped, so scraped content can't inject markup."""
    text = json.dumps(value, indent=2, ensure_ascii=False)
    out, last = [], 0
    for match in JSON_TOKEN.finditer(text):
        out.append(escape(text[last : match.start()]))
        string, colon, number, boolean, null = match.groups()
        if string is not None:
            out.append(Markup('<span class="{}">{}</span>{}').format("k" if colon else "s", string, colon or ""))
        else:
            kind, token = ("n", number) if number else ("b", boolean) if boolean else ("z", null)
            out.append(Markup('<span class="{}">{}</span>').format(kind, token))
        last = match.end()
    out.append(escape(text[last:]))
    return Markup("").join(out)


templates.env.filters["json"] = highlight_json


def sites_with_examples() -> list[dict]:
    """The site cards, each with its saved example result, for the front page and the docs."""
    return [card | {"result": EXAMPLES.get(card["host"])} for card in SITE_CARDS]


def this_page(request: Request) -> str:
    """The path and query of the request, to come back to after logging in."""
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    """The front page for visitors. Logged-in users go straight to the workbench."""
    if "url" in request.query_params:
        # Links from before the workbench moved, like /?url=…&mode=json, still open that request.
        return RedirectResponse(f"/app?{request.url.query}", status_code=303)
    if current_session(request):
        return RedirectResponse("/app", status_code=303)
    return render(request, "landing.html", {"page": "landing", "sites": sites_with_examples()})


@app.get("/app", response_class=HTMLResponse)
def workbench(request: Request, url: str = "", mode: Mode = "html", browser: BrowserOption = "auto"):
    # url/mode/browser in the query string only prefill the form, nothing is fetched on GET.
    page_session(request, this_page(request))
    context = {"page": "app", "url": url, "mode": mode, "browser": browser, "sites": SITE_CARDS}
    return render(request, "app.html", context)


@app.get("/docs", response_class=HTMLResponse)
def docs(request: Request):
    return render(request, "docs.html", {"sites": sites_with_examples(), "ip_limit": describe(IP_LIMITS)})


@app.post("/app", response_class=HTMLResponse)
def workbench_submit(
    request: Request,
    url: str = Form(...),
    mode: Mode = Form("html"),
    browser: BrowserOption = Form("auto"),
    csrf_token: str = Form(""),
):
    # Fallback for browsers without JavaScript; the page itself normally calls /scrape directly.
    page_session(request, "/app")
    context = {"page": "app", "url": url, "mode": mode, "browser": browser, "sites": SITE_CARDS}
    status_code = 200
    user_id = None
    expired = "This page has expired. Reload it and try again."
    if not same_origin(request):
        context["error"], status_code = expired, 403
    else:
        try:
            user_id = authorize(request, None, csrf_token=csrf_token)
        except HTTPException:
            context["error"], status_code = expired, 403
        except RateLimited as e:
            context["error"], status_code = f"Rate limit exceeded: {e.limit}. Wait a moment and try again.", 429
    if user_id is not None:
        try:
            result = scrape(url, mode, browser, owner=owner_of(user_id))
            context["result"] = result
            # Embedded in a <script type="application/json"> tag, so "<" is escaped: a scraped page
            # containing "</script>" must not be able to break out of that tag.
            context["seed"] = result.model_dump_json().replace("<", "\\u003c")
            if result.parsed is not None:
                context["parsed_json"] = json.dumps(result.parsed, indent=2, ensure_ascii=False)
        except ScrapeError as e:
            context["error"] = str(e)
            status_code = e.status_code
    return render(request, "app.html", context, status_code=status_code)


# ---- Sign-up, login, logout --------------------------------------------------------------------


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    if current_session(request):
        return RedirectResponse("/app", status_code=303)
    return render(request, "signup.html", {"email": ""})


@app.post("/signup", response_class=HTMLResponse)
def signup(request: Request, email: str = Form(""), password: str = Form(""), confirm: str = Form("")):
    def problem(message: str, status_code: int = 400) -> HTMLResponse:
        return render(request, "signup.html", {"email": email, "error": message}, status_code=status_code)

    if not same_origin(request):
        return problem("This form has expired. Reload the page and try again.", 403)
    normalized = users.normalize_email(email)
    if normalized is None:
        return problem("Enter a valid email address.")
    if error := users.password_problem(password):
        return problem(error)
    if password != confirm:
        return problem("The two passwords don't match.")
    # Counted only once the form is valid, so a typo doesn't use up an attempt.
    if take_signup(client_ip(request)):
        return problem("Too many sign-ups from your network today. Try again tomorrow.", 429)
    try:
        user = users.create_user(normalized, password)
    except users.EmailTaken:
        return problem("An account with this email already exists. Log in instead.", 409)
    logger.info("New account %d", user.id)
    return logged_in_redirect(request, users.start_session(user.id), "/app")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/app"):
    if current_session(request):
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", {"email": "", "next": safe_next(next)})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/app")):
    def problem(message: str, status_code: int) -> HTMLResponse:
        return render(request, "login.html", {"email": email, "next": safe_next(next), "error": message}, status_code)

    if not same_origin(request):
        return problem("This form has expired. Reload the page and try again.", 403)
    exceeded = take_login(client_ip(request), email.strip().lower())
    if exceeded:
        return problem(f"Too many attempts. Wait {seconds_until(exceeded[1])} seconds and try again.", 429)
    user = users.authenticate(email, password)
    if user is None:
        return problem("Wrong email or password.", 401)
    return logged_in_redirect(request, users.start_session(user.id), safe_next(next))


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    session = current_session(request)
    if session and same_origin(request) and tokens_match(csrf_token, session.csrf_token):
        users.end_session(request.cookies.get(SESSION_COOKIE))
        return logged_out_redirect()
    return RedirectResponse("/", status_code=303)


# ---- Dashboard: API keys and usage -------------------------------------------------------------


def dashboard_page(
    request: Request, session: users.Session, *, new_key: str | None = None, error: str | None = None, status=200
) -> HTMLResponse:
    user_id = session.user.id
    usage = user_usage(user_id)
    days = users.usage_by_day(user_id)
    context = {
        "page": "dashboard",
        "keys": auth.list_keys(user_id),
        "max_keys": MAX_KEYS_PER_USER,
        "usage": usage,
        "resets_in": seconds_until(usage.reset_at) if usage.remaining < usage.limit.amount else None,
        "days": days,
        "peak": max(n for _, n in days) or 1,
        "total": users.total_usage(user_id),
        "new_key": new_key,
        "error": error,
    }
    return render(request, "dashboard.html", context, status_code=status)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    return dashboard_page(request, page_session(request, "/dashboard"))


@app.post("/dashboard/keys", response_class=HTMLResponse)
def create_key(request: Request, name: str = Form(""), csrf_token: str = Form("")):
    session = form_session(request, csrf_token, "/dashboard")
    try:
        key, _ = auth.create_key(session.user.id, name)
    except TooManyKeys:
        return dashboard_page(
            request, session, error=f"You already have {MAX_KEYS_PER_USER} keys. Revoke one first.", status=409
        )
    # Shown on this response only: the key isn't stored anywhere, so it can't be shown again.
    response = dashboard_page(request, session, new_key=key, status=201)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/dashboard/keys/{key_id}/revoke")
def revoke_key(request: Request, key_id: int, csrf_token: str = Form("")):
    session = form_session(request, csrf_token, "/dashboard")
    auth.revoke_key(session.user.id, key_id)
    return RedirectResponse("/dashboard", status_code=303)


# ---- Account -----------------------------------------------------------------------------------


def account_page(
    request: Request, session: users.Session, *, message: str | None = None, error: str | None = None, status=200
) -> HTMLResponse:
    context = {"page": "account", "message": message, "error": error}
    return render(request, "account.html", context, status_code=status)


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    return account_page(request, page_session(request, "/account"))


def password_attempt_refused(request: Request, session: users.Session) -> str | None:
    """Password checks on the account page count like logins, so a stolen session can't be used to
    guess the password quickly."""
    exceeded = take_login(client_ip(request), session.user.email)
    return f"Too many attempts. Wait {seconds_until(exceeded[1])} seconds and try again." if exceeded else None


@app.post("/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    current: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
    csrf_token: str = Form(""),
):
    session = form_session(request, csrf_token, "/account")
    if refused := password_attempt_refused(request, session):
        return account_page(request, session, error=refused, status=429)
    if not users.check_password(session.user.id, current):
        return account_page(request, session, error="The current password is wrong.", status=400)
    if error := users.password_problem(password):
        return account_page(request, session, error=error, status=400)
    if password != confirm:
        return account_page(request, session, error="The two new passwords don't match.", status=400)
    users.change_password(session.user.id, password, keep_session=request.cookies.get(SESSION_COOKIE))
    return account_page(request, session, message="Password changed. Every other device was logged out.")


@app.post("/account/delete")
def delete_account(request: Request, password: str = Form(""), csrf_token: str = Form("")):
    session = form_session(request, csrf_token, "/account")
    if refused := password_attempt_refused(request, session):
        return account_page(request, session, error=refused, status=429)
    if not users.check_password(session.user.id, password):
        return account_page(request, session, error="The password is wrong, the account was not deleted.", status=400)
    users.delete_user(session.user.id)
    logger.info("Deleted account %d", session.user.id)
    return logged_out_redirect()
