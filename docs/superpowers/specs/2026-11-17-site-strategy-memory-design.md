# Site strategy memory and warm browsers

Date: 2026-11-17

## Why

Some sites take a long time to scrape. For a site that blocks plain requests, every visit runs the same costly steps: a plain request that gets blocked, a browser that may still have to start, a bot check solved in that browser, and a wait for the page to finish rendering. The scraper should remember what works for each site and skip the steps it already knows will fail, and its browsers should be ready before the first request arrives.

## Where the time goes today

Measured locally on 2026-11-17 against `https://www.imdb.com/title/tt0111161/`, no proxy:

| Step | Time |
|---|---|
| Plain request that gets blocked (AWS WAF, status 202) | 0.3 s |
| Starting the Playwright driver and Chromium (first browser use in a worker) | 1.2 s |
| New browser context and page | 0.1 s |
| Solving the AWS WAF check in the browser | 2.2 s |
| HTML mode: waiting for the page to settle (`networkidle`, up to 5 s) | 3.2 s |
| JSON mode: same page, stopped early by the parser's ready check | 0.0 s |
| **Plain request with the cookies from a passed check** | **0.9–1.0 s total** |

First request, HTML mode: 6.9 s. The same key's next request: 1.0 s. A new key in JSON mode: 3.1 s, then 0.9 s.

Conclusions that shape the design:

1. "IMDb always needs the browser" is the wrong rule. After one browser visit per key, plain HTTP with that visit's cookies is 3 to 7 times faster than the browser. The scraper already does this (`blocking.py`, `Clearance`); the memory must keep preferring cookie-backed plain requests over the browser.
2. The largest avoidable cost is losing those cookies. They live in one process's memory for 20 minutes, and Fly stops the machine when it's idle (`min_machines_running = 0`), so each key pays the bot check again after every wake-up.
3. The site memory (`needs-browser:{site}`) lasts only 10 minutes and only knows "blocked". It doesn't know "the plain page loads but the data only appears after JavaScript runs" (the parse-failure retry in `service.py`), so that slow path, a plain fetch, a failed parse and then the browser, repeats on every request.
4. Browsers start lazily on first use per worker thread (`get_browser`), so the first browser request after every boot pays about 1.2 s locally, likely more on a shared Fly CPU.

## Goals

- Remember per site, for days rather than minutes, whether plain requests get blocked, and per parser whether its data needs the browser to render. Learn this from real outcomes, shared across machines through Redis, and survive restarts.
- Start known parser sites from a correct guess (IMDb starts as "blocks plain requests") so the very first request is fast too.
- Notice when a site stops blocking, without making a user's request pay for the check.
- Keep each API key's bot-check cookies in Redis as well, so they survive restarts and scale-to-zero and are shared across machines, still strictly per key.
- Have Chromium already running in each browser worker shortly after boot.

## Non-goals

- Shared bot-check cookies across keys, or solving bot checks ahead of time in the background. Cookies stay per key (decided).
- A spare pre-made browser context. Measured at 0.1 s, and a context depends on the URL (proxy or direct) and the caller (cookies), so a spare would rarely fit.
- Pre-opening TLS connections to sites.
- Choosing strategies by speed scoring (approach C below).
- Shortening the 5 s HTML-mode render wait. It's 3.2 s of the IMDb first fetch, but cutting it changes how complete HTML results are. Worth its own decision later.
- An admin endpoint to view or edit profiles.

## Anti-goals

- Any change that makes a request slower than today in the worst case. A wrong memory must cost at most one fallback step, as `fetch_known_blocked` already guarantees.
- One key's cookies ever being sent on another key's request.
- Background work that keeps hitting a site: at most one background check per site per day.

## Constraints

- No working proxy right now (the plan is used up). All local testing runs with `PROXY_URL` unset, so everything goes out directly. The proxy code (`proxy.py`, `context_proxy`, `proxy_for`) stays as it is and keeps working in production.
- The local `REDIS_URL` (`redis://localhost:6379`) points at a Redis server older than 6. redis-py 8.1 sends `HELLO`, the server rejects it, and the cache pauses itself, so locally everything runs on the in-memory fallback. Testing the Redis paths locally needs Redis 6 or newer (for example `docker run -p 6379:6379 redis:7`) or the existing `fakeredis` tests. Fixing the local server is not part of this work.
- Fly VM: 1 shared CPU and 1 GB of RAM, scale-to-zero. Warming must not delay the server accepting requests.
- Tests stay offline (no network, Redis or browser), as `tests/conftest.py` says.
- Code style follows the existing modules: plain functions, short docstrings that say why, best-effort Redis through `cache.py`.

## Approaches considered

- **A. Learned site profiles, pre-filled from parsers (chosen).** One record per site, learned from outcomes, kept in Redis with an in-memory copy, re-checked about once a day. Parser definitions give the starting guess for their sites. It learns new sites by itself, stays correct when sites change, and fixes IMDb's very first request.
- **B. Hand-written site list.** A config or parser attribute plus today's 10-minute flag. Simple and predictable, but manual, blind to sites nobody listed, and silently wrong when a site changes.
- **C. Speed scoring.** Track the success rate and latency of each strategy per site and pick the fastest. With only two real strategies it adds complexity for little gain over A.

For pre-warming, the options were ready browsers only (chosen), adding server-earned bot-check cookies shared by all keys (rejected: it breaks the per-key cookie rule and spends background browser runs and proxy traffic), and also warming TLS connections (rejected: small gain).

## Design

### 1. Site profiles (`scraper/profiles.py`, new)

A profile per site (`site_of(url)`, e.g. `imdb.com`):

```python
@dataclass
class SiteProfile:
    blocks_plain: bool = False          # plain requests without cookies get a block or bot check
    render: set[str] = field(default_factory=set)  # parsers whose data needs the browser here
    checked_at: float = 0.0             # wall clock: last time the plain route was tested
    seeded: bool = False                # True while the profile is only a parser's guess
```

`render` holds parser names. Add a `name` to each `Parser`, taken from its parse function (`parse_movie`, `parse_book`), so the names are stable and unique.

Storage: the Redis key `scrape:v2:site:{site}` holds compact JSON, with a TTL of `PROFILE_SECONDS = 7 * 24 * 3600` refreshed on every write. A site nobody scrapes for 7 days is forgotten, and relearning costs one blocked request (0.3 s). An in-memory dict mirrors it: on a read, an entry younger than `PROFILE_MEMORY_SECONDS = 60` is used without asking Redis, which keeps the per-request cost to at most one Redis read a minute per site and lets other machines' updates show up within a minute. The dict size is capped the same way `MAX_CLEARANCES` caps clearances. Without Redis, the memory copy is the only copy. Reading and writing go through new `cache.get_json(name)` and `cache.set_json(name, value, ttl)` helpers, built on the existing `_get` and `_set`, so the pause-on-error behaviour applies.

Seeds: `Parser` gets `blocks_plain: bool = False`, set to `True` for the IMDb parser. `get_profile(site)` returns the learned profile if one exists. Otherwise, if any parser for that host has `blocks_plain`, it returns a seeded profile (`blocks_plain=True, seeded=True, checked_at=now`), which is not stored until something is learned. A seed never overrides a learned profile.

Updates are small functions that read, change and write the record: `learn_blocked(site)`, `learn_not_blocked(site)`, `learn_needs_render(site, parser_name)`, `learn_renders_plain(site, parser_name)`. Two machines writing at once can lose one update; the next outcome corrects it, which is acceptable for a cache of hints.

This replaces `needs_browser`, `remember_needs_browser` and `forget_needs_browser` in `blocking.py` and the `needs-browser:` flags. The old flags simply expire within 10 minutes. `cache.raise_flag`, `flag_is_up` and `lower_flag` are used only by those functions, so they are removed too, and `tests/conftest.py` clears the profile memory instead of `cache._flags`. Profile keys use `site_of`, which already drops `www.`, so `www.imdb.com` and the parser host `imdb.com` match.

### 2. Choosing how to fetch (`fetch.py`, `service.py`)

The order in `fetch()` stays as it is and only reads the profile instead of the flag:

1. `browser="always"`: browser.
2. The owner has cookies for the site that haven't failed plain HTTP: plain request with them (0.9 s on IMDb). **This stays ahead of the profile**, because it's the fastest route for blocking sites.
3. The profile says `blocks_plain`: `fetch_known_blocked`, which tries the browser first and still falls back to plain HTTP if the browser fails, as today.
4. Otherwise: plain request; if it's blocked, the browser, then `learn_blocked(site)`.

In `fetch_known_blocked`, the existing "the site answered plainly again" branch calls `learn_not_blocked(site)` instead of `forget_needs_browser`.

In `service.scrape`, JSON mode: if `browser == "auto"`, the browser is enabled, and the profile's `render` contains `parser.name`, the plain fetch is skipped and the page is fetched with `fetch_with_browser` (with `ready`, inside `gated()` like every other network fetch) straight away. In the existing parse-failure branch, when the browser page parses, call `learn_needs_render(site, parser.name)`. The page cache keeps its current keys; a cached page is still used first.

Cookie interplay: rendering is about JavaScript, not bot checks, so `render` applies whether or not the owner has cookies.

### 3. Daily re-check (background)

After a request that a profile sent past the plain route (step 3, or the `render` shortcut) has been served, if `now - checked_at > RECHECK_SECONDS` (24 h), the scraper schedules one background check on a single-thread `ThreadPoolExecutor` (`recheck_pool`):

- Claim first: write `checked_at = now` to the profile before the check runs, so other requests and other machines don't start their own. In process, a set of sites with checks in flight guards against duplicates too.
- Blocking check: `fetch_page(url)` with no cookies. If it's not `looks_blocked`, call `learn_not_blocked(site)`.
- Render check: `fetch_page(url)`, then `parse_page(parser, page)`. If that succeeds, call `learn_renders_plain(site, parser.name)`.
- The URL checked is the one just requested, so it has already passed URL validation, and `fetch_page` validates again, including every redirect hop. Any exception is logged at INFO and ignored.
- A seeded profile's `checked_at` starts at "now" on each read, so a seed is never re-checked until a real outcome has been stored.

Cost: at most one plain request per site per day, through the proxy in production.

### 4. Per-key bot-check cookies in Redis (`blocking.py`)

`Clearance` keeps its fields but its expiry becomes wall-clock time (`time.time()`), since it now crosses processes. Storage:

- Redis key `scrape:v2:clearance:{owner}:{site}`, where `owner` is already a hash of the API key (`quota.key_id`). The value is JSON `{cookies, headers, expires, http_works}`, with a TTL equal to the time left until `expires`.
- `CLEARANCE_SECONDS` goes from 20 minutes to `4 * 3600`. Cookies whose own `expires` is set and already past are dropped when loaded. If the stored cookies stop working, plain HTTP with them gets blocked, `note_http_result(False)` records that, and the request goes through the browser, which earns fresh cookies: one 0.3 s step, as today.
- `get_clearance` reads memory first, then Redis, and fills memory from Redis. `save_clearance` and `note_http_result` write to both. `note_http_result` currently only flips a field on the in-memory object, so it now also needs the owner and site to rewrite the Redis entry. An owner of `None` is still never stored.
- The rule stays: cookies are looked up only by the requesting owner's own hash.

Cookies are third-party site cookies, not user credentials, and Redis already holds cached pages. The docs page states that they're stored per key for up to 4 hours.

### 5. Warm browsers (`browser.py`, `app.py`)

- `warm_browsers()` in `browser.py` submits `BROWSER_WORKERS` tasks to `browser_pool`. Each task waits on a `threading.Barrier(BROWSER_WORKERS, timeout=30)` and then calls `get_browser()`. The barrier makes every worker thread take exactly one task, so each thread starts its own browser (the sync Playwright API is bound to its thread). A broken barrier or a launch failure is logged and ignored; the first real request retries, as today.
- `app.py` gains a FastAPI `lifespan` that, when `BROWSER_ENABLED` and `BROWSER_WARM` (env, default `on`) isn't `off`, starts `warm_browsers` on a daemon thread and returns at once, so the server accepts requests immediately. A browser request that arrives during warm-up queues behind the warm task in its worker and gets the browser it started.
- Warm tasks don't take `browser_slots`, because they don't load pages.
- `tests/conftest.py` sets `BROWSER_WARM=off`.
- Memory: two idle Chromium processes fit in the 1 GB VM. Production verification checks for out-of-memory restarts.

### 6. Visibility

- The fetch log line names the route taken and the time spent, for example `imdb.com: plain+cookies 0.94 s` or `imdb.com: browser (profile) 3.1 s`, so routes can be verified from logs.
- README and `docs.html`: replace "remembers for 10 minutes" and "kept for 20 minutes" with the new behaviour. Mention warm browsers in the README features list.

## Testing

Offline unit tests (pytest, `fakeredis` where Redis matters), in the style of `tests/test_fetch.py`:

- Profiles: a blocked plain fetch is remembered, and the next request goes to the browser without a plain attempt; a profile survives clearing the memory copy when Redis holds it; the IMDb seed sends the first request to the browser; a learned "not blocked" overrides the seed; the TTL is set.
- Render: a parse failure on the plain page plus browser success records the parser, the next JSON request skips the plain fetch, and HTML mode for the same site is unaffected.
- Cookies before profile: an owner with working cookies still gets a plain request even when the profile says `blocks_plain`.
- Re-check: runs only when stale, runs only once while one is in flight, and clears `blocks_plain` or `render` on success; nothing changes on failure.
- Clearances: saved to Redis and found by a fresh process (memory cleared); never found under another owner; expired cookies dropped; `http_works=False` persists.
- Warm-up: with a fake `get_browser`, `warm_browsers` calls it once per worker thread; `BROWSER_WARM=off` skips it.
- Existing tests keep passing; `fetch_known_blocked` behaviour is unchanged.

Manual, local, no proxy, Redis 6 or newer if available: repeat the IMDb timing from "Where the time goes" and expect

- the first browser request after boot takes no driver or launch time,
- the first IMDb request makes no plain attempt (seed),
- a second key's IMDb request skips the plain attempt,
- after a restart with Redis, a key's IMDb request goes plain with cookies (about 1 s).

## Left to the builder

- Exact JSON field names and the in-memory size cap for profiles.
- Log wording.
- The order of the work, as long as each step leaves the tests green.

## Calls made during the design (for review)

- Cookie-backed plain requests stay ahead of the profile, so IMDb isn't sent to the browser every time.
- Profiles last 7 days, with re-checks every 24 h in the background.
- Render memory is per parser, not per site.
- Cookies are kept for up to 4 hours instead of 20 minutes, falling back to the browser when they stop working.
- No spare browser context, based on the measured 0.1 s.
- Warm-up runs off the startup path, and `BROWSER_WARM=off` turns it off.
