# Universal Scraper API

A web-scraping API: send it any public URL and get back clean HTML, or structured JSON for supported sites. Plain HTTP is tried first, and it falls back to a headless Chromium browser when a site's bot protection blocks plain requests.

![Python](https://img.shields.io/badge/python-3.12-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-009688) ![Redis](https://img.shields.io/badge/Redis-cache%20%26%20quotas-DC382D) ![tests](https://img.shields.io/badge/tests-53%20passing-brightgreen) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

## Features

- **Two output modes**: `html` returns the cleaned page; `json` returns structured fields on supported sites (books, products, recipes, movies and more), using site parsers and schema.org JSON-LD.
- **Automatic browser fallback**: it spots blocks (403/429/503, Cloudflare, AWS WAF and DataDome challenge pages), retries in Playwright Chromium, and remembers for 10 minutes which sites need the browser. Clearance cookies from passed checks are reused for 20 minutes.
- **Browser-grade HTTP**: `curl_cffi` with a current browser TLS fingerprint, request timeouts, a response-size cap, and a content-type check.
- **Batch endpoint**: up to 50 URLs per call, run in parallel with per-site concurrency limits and spacing so no single site gets hammered. Failures are reported per URL.
- **Caching**: Redis caches pages (10 min for successes, 30 s for errors). If Redis goes down, the API skips it for 30 s instead of timing out on every request.
- **Security**:
  - API keys are compared in constant time, and every request is refused if no keys are configured.
  - Rate limits apply per IP before a key is accepted, and per key after.
  - SSRF protection: every URL and every redirect hop is checked, so requests can't reach private, loopback or cloud-metadata addresses.
- **Web workbench** at `/` and hand-written API docs at `/docs`.

## Usage

```bash
curl -H "X-API-Key: $SCRAPER_API_KEY" \
  "http://127.0.0.1:8000/scrape?url=https://books.toscrape.com/&mode=json"
```

| Parameter | Values | Default |
|---|---|---|
| `url` | any public http(s) URL | required |
| `mode` | `html`, `json` | `html` |
| `browser` | `auto` (fallback when blocked), `never`, `always` | `auto` |

`POST /scrape/batch` takes `{"urls": [...], "mode": "...", "browser": "..."}`. Python client example: [`examples/api_client.py`](examples/api_client.py).

### Sites with structured JSON parsers

books.toscrape.com · quotes.toscrape.com · webscraper.io test cars · scrapeme.live · allrecipes.com · imdb.com · knjizare-vulkan.rs · iponcomp.com

## Run it

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium                      # only needed for the browser fallback
export API_KEYS="devkey"                         # Windows PowerShell: $env:API_KEYS = "devkey"
uvicorn main:app --reload
```

Set `REDIS_URL` to turn on shared caching and quotas. Without it, the app runs on in-memory counters.

Set `PROXY_URL` (`http://user:password@host:port`) to send both plain HTTP and browser requests out through a proxy. Data-center IPs, like a hosted server's, are often blocked, and a residential proxy usually gets through. `/health` shows whether a proxy is on.

**Docker / Fly.io**: the multi-stage `Dockerfile` installs Chromium into a fixed path, and `fly.toml` deploys it with scale-to-zero. API keys are set with `fly secrets set API_KEYS=...` (see [`docs/api-keys.md`](docs/api-keys.md)).

**Tests** (offline, no network or Redis needed):

```bash
pytest
```

## Project layout

```
scraper/
  app.py         routes, auth and rate limiting
  service.py     single + batch scraping, cache lookups
  fetch.py       HTTP-first, browser-fallback orchestration
  http_fetch.py  curl_cffi client, redirects, size and type checks
  browser.py     Playwright Chromium fetches, queued
  blocking.py    bot-check detection, clearance-cookie reuse
  parsers.py     per-site structured parsers
  proxy.py       optional outgoing proxy for both fetch paths
  urls.py        URL normalisation and SSRF guard
  cache.py       Redis cache with graceful fallback
  quota.py       per-IP and per-key limits
```

## Stack

Python · FastAPI · curl_cffi · Playwright · BeautifulSoup + lxml · Redis · `limits` · Docker · Fly.io · pytest · ruff
