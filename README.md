# Universal Scraper API

A web-scraping API: send it any public URL and get back clean HTML, or structured JSON for supported sites. Plain HTTP is tried first, and a headless Chromium takes over when a site's bot protection blocks plain requests.

[![tests](https://github.com/ihabND/ihabov_universal_scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/ihabND/ihabov_universal_scraper/actions/workflows/tests.yml) ![Python](https://img.shields.io/badge/python-3.12-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-009688) ![Redis](https://img.shields.io/badge/Redis-cache%20%26%20quotas-DC382D) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

![The home page scraping an IMDb title: plain HTTP is bot-checked, so step 3 of the ladder, the headless browser, serves it](docs/images/demo.gif)

**Live demo: [ihabov-universal-scraper.fly.dev](https://ihabov-universal-scraper.fly.dev)**. The home page scrapes the example sites live with a public test key, no sign-up needed; other pages need a full API key. The server scales to zero when idle, so the first request after a quiet spell takes a few seconds while it starts.

## Features

- **Two output modes**: `html` returns the cleaned page; `json` returns structured fields from site parsers (books, products, recipes, movies and more), and on any other page the structured data it carries for its own scripts (JSON-LD, Next.js data, YouTube's `ytInitialData`, initial state).
- **A ladder of fetchers**: plain HTTP with curl's Chrome fingerprint (`curl_cffi`), the same with its Safari fingerprint, then Playwright Chromium, then Patchright Chromium (a Playwright fork patched against automation detection) for sites that still block the browser. A request moves up when it meets a bot check (Cloudflare, AWS WAF, DataDome, and the checks of Reddit, Yandex and Expedia Group) or a blocking status, when a page comes back nearly empty because JavaScript builds it, or when JSON mode finds the data only after JavaScript runs. Captchas that no registered solver can pass end the wait at once.
- **Site strategies**: every site gets a permanent record of what works there and of every setting used to scrape it. The scraper fills it in as it goes; values set by hand are pinned and never changed. A route that fails falls back to the others in the same request. See [Site strategies](#site-strategies).
- **Per-key bot-check cookies**: cookies from a passed bot check are kept per API key for up to 4 hours, so later pages from that site load over plain HTTP (IMDb title pages in about 1.5 s). One key's cookies are never sent for another key. Cookies that plain HTTP was blocked with aren't reused, and Akamai Bot Manager's aren't kept at all.
- **Batch endpoint**: up to 50 URLs per call, 8 at a time per key, at most 4 per site at once. Failures are reported per URL.
- **Caching**: fetched pages are cached in Redis for 10 minutes (30 s for errors), shared by both modes, and identical requests in flight share one fetch.
- **Security**: keys compared in constant time; rate limits per IP before a key is accepted and per key after; every connection the scraper makes itself, the browser's included, passes a local egress guard that only connects to public addresses, which also covers redirects and DNS rebinding. With `PROXY_URL` set, traffic to sites outside `PROXY_BYPASS` goes to that proxy instead, which must not be able to reach your internal network.
- **Home page** at `/`: try it on the example sites with the public test key, see the raw response and which step of the ladder served it. The full **web workbench** is at `/workbench`, and hand-written API docs at `/docs`.

## Usage

```bash
curl -H "X-API-Key: $SCRAPER_API_KEY" \
  "http://127.0.0.1:8000/scrape?url=https://books.toscrape.com/&mode=json"
```

| Parameter | Values | Default |
|---|---|---|
| `url` | any public http(s) URL | required |
| `mode` | `html`, `json` | `html` |
| `browser` | `auto` (browser when needed), `never`, `always` | `auto` |

`POST /scrape/batch` takes `{"urls": [...], "mode": "...", "browser": "..."}`. Python client example: [`examples/api_client.py`](examples/api_client.py).

### Sites with structured JSON parsers

webscraper.io test cars · scrapeme.live · books.toscrape.com · quotes.toscrape.com · knjizare-vulkan.rs · iponcomp.com · allrecipes.com · imdb.com

Any other page in JSON mode gives its embedded structured data, or a 502 when it has none.

## Site strategies

Every site the scraper has fetched successfully gets a permanent record in a SQLite file (`STRATEGY_DB`): the fetcher to start with, the pages that need JavaScript, and every other setting a scrape uses there: fingerprints, proxy choice, retries, timeouts, browser waits, skipped resources, locale, headers, URL parameters, batch pacing and cache lifetimes. What the scraper learns is written with the date and what it saw (`http got 403 (cloudflare check)`). A value set by hand is pinned: the scraper never changes it, and when a check disagrees with it the site gets a flag. Once a day, a site that skips plain HTTP gets it tried again in the background, after a request has been served.

`/sites` shows every site grouped by domain, with each setting's value and where it came from. Viewing takes an API key; pinning, unpinning, clearing flags, deleting a site and importing pins take a key from `ADMIN_KEYS`. The same as an API: `GET /api/sites` with `X-API-Key`; with `X-Admin-Key`, `PUT /api/sites/{site}/pins` (`{"pins": {"start": "browser"}}`, replacing all of the site's pins), `DELETE /api/sites/{site}/flags`, `DELETE /api/sites/{site}`, `GET /api/sites/export` (every site's whole record) and `POST /api/sites/import` (an exported file; only each site's `pinned` values are applied, replacing its pins).

## Run it

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium                      # only needed for the browser
export API_KEYS="devkey"                         # Windows PowerShell: $env:API_KEYS = "devkey"
uvicorn scraper.app:app --reload
```

| Setting | Meaning | Default |
|---|---|---|
| `API_KEYS` | comma-separated keys; without any, every request is refused | none |
| `REDIS_URL` | page cache, shared quotas and bot-check cookies across restarts | in-memory |
| `PROXY_URL` | send traffic out through a proxy (`http://user:password@host:port`) | direct |
| `PROXY_BYPASS` | sites that skip the proxy | practice sites |
| `IP_RATE_LIMIT`, `KEY_RATE_LIMITS` | limits for wrong keys and per key | `10/minute`, `300/minute;10000/day` |
| `RATE_LIMIT_FAIL_OPEN` | allow requests while the rate-limit store is down | `on` |
| `BROWSER_FALLBACK`, `BROWSER_WARM` | use the browser at all; start it at boot | `on`, `on` |
| `BROWSER_ENGINE` | engine of the browser tier: `playwright`, or `patchright` (a stealthier fork) | `playwright` |
| `STEALTH_BROWSER` | a Patchright tier after the browser, started on first use; needs `BROWSER_FALLBACK` | `on` |
| `BROWSER_WORKERS`, `BATCH_WORKERS` | pages open at once; batch URLs at once per key | `2`, `8` |
| `STRATEGY_DB` | the SQLite file holding every site's strategy | `data/strategies.db` |
| `ADMIN_KEYS` | comma-separated keys that may edit site strategies | none |
| `TEST_KEY` | a public key for the home page: `GET /scrape` on the example sites only; unset turns the live demo off | none |
| `TEST_KEY_SITES` | sites the test key may scrape (subdomains included) | practice sites |
| `TEST_KEY_LIMITS`, `TEST_KEY_TOTAL_LIMITS` | the test key's limits per client IP and for everyone together; they never fail open | `30/hour`, `1000/day` |
| `TRUSTED_IP_HEADER` | header carrying the client IP from a trusted proxy (`Fly-Client-IP` on Fly) | none |

**Docker / Fly.io**: the `Dockerfile` installs Chromium and runs as a non-root user, and `fly.toml` deploys it with scale-to-zero and a health check. API keys are set with `fly secrets set API_KEYS=...` (see [`docs/api-keys.md`](docs/api-keys.md)), admin keys with `fly secrets set ADMIN_KEYS=...`, and the home page's public test key with `fly secrets set TEST_KEY=...`. Site strategies live on a volume mounted at `/data`. Create it once before the first deploy with this change, then give the app's user the folder:

```bash
fly volumes create scraper_data --size 1 --region ams
fly deploy
fly ssh console -C "chown scraper /data"
```

Until the `chown`, scraping works with the default settings and the log says the store can't be written.

**Tests** run offline, with no network, Redis or browser: `pytest`. `pytest -m browser` adds the tests that drive a local Chromium, once with Playwright and once with Patchright.

## Project layout

```
scraper/
  app.py        the FastAPI app and the scraper's lifetime
  settings.py   every environment setting, read once
  api/          routes, key check and rate limits, request/response models
  web/          the home page, the workbench and the docs pages
  core/         scrape and batch (service.py), the fetcher ladder (ladder.py), Page, errors
  fetchers/     plain HTTP and the browser; a new tier is a new class here
  antibot/      bot-check detection, per-key cookies, the captcha-solver plug-in point
  parsers/      one module per site, plus shared JSON-LD readers
  store/        Redis cache, quotas, site strategies (SQLite)
  net/          URL checks, proxy choice, the egress guard
```

Adding a site: write `scraper/parsers/<site>.py` with an `@parser(...)` function (or register a JSON-LD reader), import it in `parsers/__init__.py`, and save a page plus its expected output in `tests/fixtures/parsers/`.

## Stack

Python · FastAPI · curl_cffi · Playwright · BeautifulSoup + lxml · Redis · `limits` · Docker · Fly.io · pytest · ruff
