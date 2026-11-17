# Site Strategy Memory and Warm Browsers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make repeat scrapes of slow sites fast. The scraper remembers per site, for days, which route works; keeps each API key's bot-check cookies in Redis; and starts Chromium in the background at boot.

**Architecture:** A new `scraper/profiles.py` holds one learned record per site (blocks plain requests? which parsers need the browser to render?). It is stored as JSON in Redis through new `cache.get_json`/`cache.set_json` helpers, mirrored in memory, pre-filled from a `Parser.blocks_plain` flag, and re-checked in the background once a day. `fetch.py` and `service.py` read it in place of today's 10-minute `needs-browser` flag. `blocking.py` writes clearances through to Redis. `browser.py` gains `warm_browsers()`, started from a FastAPI lifespan.

**Tech Stack:** Python 3.12+, FastAPI, Playwright (sync), curl_cffi, redis-py, pytest, fakeredis, ruff.

**Spec:** `docs/superpowers/specs/2026-11-17-site-strategy-memory-design.md`

## Global Constraints

- Tests stay offline: no network, Redis server or browser. Redis behaviour is tested with `fakeredis`.
- Local manual testing runs with `PROXY_URL` unset. Don't change `scraper/proxy.py` or how `context_proxy`/`proxy_for` are called.
- Redis is best effort: every Redis call goes through `cache._get`/`cache._set` (or the new helpers built on them), so a Redis error pauses Redis and never fails a request.
- `PROFILE_SECONDS = 7 * 24 * 3600`, `PROFILE_MEMORY_SECONDS = 60`, `RECHECK_SECONDS = 24 * 3600`, `CLEARANCE_SECONDS = 4 * 3600`, `WARM_BARRIER_SECONDS = 30`.
- Redis key names: `scrape:v2:site:{site}` for profiles and `scrape:v2:clearance:{owner}:{site}` for clearances (`KEY_PREFIX` is `scrape:v2`).
- Bot-check cookies are only ever looked up under the requesting owner's own hash.
- Cookie-backed plain requests are tried before any profile-driven browser route.
- No request may be slower in the worst case than today: a wrong profile costs at most one fallback step.
- Style: plain functions, docstrings that say why, line length 120, `ruff check .` clean.
- Work on a branch `feature/site-strategy-memory`, not `main`. End every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- Redis down or paused while learning or reading a profile or clearance: the request still succeeds, using memory only. Tested in Task 2 and Task 5.
- A broken or old-shaped JSON value under a profile or clearance key: treated as "no record", never a 500. Tested in Task 2 and Task 5.
- `browser="never"` (or `BROWSER_FALLBACK=off`) on a site whose profile or seed says `blocks_plain`: plain fetch only, no browser. Tested in Task 3.
- A background re-check that raises (`FetchError`, `UnsupportedContentError`, any `Exception`) or finds the site still blocked: nothing crashes and the profile keeps its route. Tested in Task 4.
- Chromium failing to start at boot (for example, not installed locally): the server still starts and `warm_browsers` returns 0. Tested in Task 6.

---

### Task 1: JSON helpers in the cache, and a shared Redis test fixture

**Files:**
- Modify: `scraper/cache.py` (add helpers after `set_cached_page`)
- Modify: `tests/conftest.py` (add the `redis` fixture)
- Modify: `tests/test_cache.py:15-20` (remove its local `redis` fixture, now in conftest)
- Test: `tests/test_cache.py`

**Interfaces:**
- Produces: `cache.get_json(name: str) -> dict | None` and `cache.set_json(name: str, value: dict, ttl: int) -> None`, stored at `f"{KEY_PREFIX}:{name}"` as uncompressed UTF-8 JSON. `get_json` returns `None` for a missing key, unparseable bytes or a non-dict value. `cache.redis_available() -> bool` is `True` when a client is configured and not paused (`_redis() is not None`). Profiles use it to tell "Redis says there is no record" apart from "there is no Redis to ask". The `redis` pytest fixture (fakeredis client patched into `cache.redis_client`, `_paused_until` reset).

- [ ] **Step 1: Install dev dependencies** (`fakeredis` is missing locally)

Run: `python -m pip install -e ".[dev]"`, then `python -m pytest -q`
Expected: all tests pass, including `tests/test_cache.py`.

- [ ] **Step 2: Create the branch**

Run: `git switch -c feature/site-strategy-memory`

- [ ] **Step 3: Move the `redis` fixture to `tests/conftest.py`**, unchanged, and delete it from `tests/test_cache.py`.

- [ ] **Step 4: Write the failing tests** in `tests/test_cache.py`

```python
def test_json_values_round_trip_with_a_ttl(redis):
    cache.set_json("site:shop.example", {"b": True, "r": ["parse_movie"]}, 600)
    assert cache.get_json("site:shop.example") == {"b": True, "r": ["parse_movie"]}
    assert 0 < cache.redis_client.ttl("scrape:v2:site:shop.example") <= 600


def test_broken_json_values_read_as_missing(redis):
    cache.redis_client.set("scrape:v2:site:bad.example", b"\xff not json")
    cache.redis_client.set("scrape:v2:site:list.example", b"[1, 2]")
    assert cache.get_json("site:bad.example") is None
    assert cache.get_json("site:list.example") is None
    assert cache.get_json("site:missing.example") is None


def test_json_helpers_do_nothing_without_redis():
    cache.set_json("site:shop.example", {"b": True}, 600)  # no fixture: redis_client is None
    assert cache.get_json("site:shop.example") is None
    assert not cache.redis_available()


def test_redis_is_unavailable_while_paused(redis, monkeypatch):
    assert cache.redis_available()
    monkeypatch.setattr(cache, "_paused_until", time.monotonic() + 30)
    assert not cache.redis_available()
```

- [ ] **Step 5: Run them to verify they fail**

Run: `python -m pytest tests/test_cache.py -q -k json`
Expected: FAIL with `AttributeError: module 'scraper.cache' has no attribute 'set_json'` (add `import time` to the test module for the pause test)

- [ ] **Step 6: Implement `get_json`, `set_json` and `redis_available` in `scraper/cache.py`** on top of `_get`/`_set`. Catch `UnicodeDecodeError` and `ValueError` (which includes `json.JSONDecodeError`) and log at WARNING, as `get_cached` does.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest -q` and `python -m ruff check .`
Expected: all pass, ruff clean.

- [ ] **Step 8: Commit**

```bash
git add scraper/cache.py tests/conftest.py tests/test_cache.py
git commit -m "Add JSON helpers to the Redis cache"
```

---

### Task 2: Site profiles with parser seeds

**Files:**
- Create: `scraper/profiles.py`
- Modify: `scraper/parsers.py:20-33` (`Parser` gains `blocks_plain` and a `name` property) and the IMDb entry (`blocks_plain=True`)
- Modify: `tests/conftest.py` (`clean_state` also calls `profiles.clear_memory()`)
- Test: `tests/test_profiles.py` (create)

**Interfaces:**
- Consumes: `cache.get_json`, `cache.set_json` (Task 1); `parsers.PARSERS`.
- Produces:
  - `Parser.blocks_plain: bool = False` field; `Parser.name -> str` property returning `self.parse.__name__` (e.g. `"parse_movie"`).
  - `@dataclass SiteProfile(blocks_plain: bool = False, render: set[str] = field(default_factory=set), checked_at: float = 0.0, seeded: bool = False)`. `checked_at` is wall clock (`time.time()`).
  - `get_profile(site: str) -> SiteProfile` returns a copy that is safe to mutate. A learned record wins; with no record, a host whose parser has `blocks_plain` gives `SiteProfile(blocks_plain=True, seeded=True, checked_at=time.time())`; otherwise `SiteProfile()`.
  - `learn_blocked(site)`, `learn_not_blocked(site)`, `learn_needs_render(site, parser_name: str)`, `learn_renders_plain(site, parser_name: str)`, all `-> None`. Each starts from `get_profile(site)` (seed values included), changes one field, sets `seeded=False` and `checked_at=time.time()`, and saves.
  - `save_profile(site: str, profile: SiteProfile) -> None` writes memory and Redis (`cache.set_json(f"site:{site}", ..., PROFILE_SECONDS)`), JSON shape `{"b": bool, "r": [sorted names], "c": float}`.
  - `clear_memory() -> None` for tests.
  - Constants `PROFILE_SECONDS`, `PROFILE_MEMORY_SECONDS`, `MAX_PROFILES = 1000`.

- [ ] **Step 1: Write the failing tests** in `tests/test_profiles.py`

```python
import time

from scraper import cache, profiles
from scraper.profiles import SiteProfile, get_profile


def test_an_unknown_site_has_an_empty_profile():
    assert get_profile("shop.example") == SiteProfile()


def test_imdb_starts_seeded_as_blocking():
    profile = get_profile("imdb.com")
    assert profile.blocks_plain and profile.seeded


def test_learning_overrides_the_seed():
    profiles.learn_not_blocked("imdb.com")
    profile = get_profile("imdb.com")
    assert not profile.blocks_plain and not profile.seeded


def test_without_redis_a_learned_profile_outlives_the_memory_minute(monkeypatch):
    profiles.learn_blocked("shop.example")
    later = time.monotonic() + profiles.PROFILE_MEMORY_SECONDS + 1
    monkeypatch.setattr(profiles.time, "monotonic", lambda: later)
    assert get_profile("shop.example").blocks_plain


def test_learning_render_keeps_the_seeded_block():
    profiles.learn_needs_render("imdb.com", "parse_movie")
    profile = get_profile("imdb.com")
    assert profile.blocks_plain and profile.render == {"parse_movie"} and not profile.seeded


def test_profiles_are_shared_through_redis(redis):
    profiles.learn_blocked("shop.example")
    profiles.clear_memory()  # another machine, or after a restart
    assert get_profile("shop.example").blocks_plain
    assert 0 < cache.redis_client.ttl("scrape:v2:site:shop.example") <= profiles.PROFILE_SECONDS


def test_memory_is_trusted_for_a_minute_then_redis_is_read_again(redis, monkeypatch):
    profiles.learn_blocked("shop.example")
    cache.set_json("site:shop.example", {"b": False, "r": [], "c": time.time()}, 600)  # another machine learned
    assert get_profile("shop.example").blocks_plain
    later = time.monotonic() + profiles.PROFILE_MEMORY_SECONDS + 1
    monkeypatch.setattr(profiles.time, "monotonic", lambda: later)
    assert not get_profile("shop.example").blocks_plain


def test_profiles_work_in_memory_while_redis_is_paused(redis, monkeypatch):
    monkeypatch.setattr(cache, "_paused_until", time.monotonic() + 30)
    profiles.learn_blocked("shop.example")
    assert get_profile("shop.example").blocks_plain


def test_a_broken_redis_record_reads_as_unknown(redis):
    cache.set_json("site:shop.example", {"b": "yes", "r": 5}, 600)
    assert get_profile("shop.example") == SiteProfile()


def test_returned_profiles_are_copies():
    get_profile("shop.example").render.add("parse_book")
    assert get_profile("shop.example").render == set()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_profiles.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'scraper.profiles'`

- [ ] **Step 3: Add `blocks_plain` and `name` to `Parser`**, set `blocks_plain=True` on the IMDb parser, and keep its label "Title page, uses the browser".

- [ ] **Step 4: Implement `scraper/profiles.py`** per the Interfaces block. Memory is `dict[str, tuple[float, SiteProfile | None, float]]` keyed by site, with the value being `(time.monotonic() when loaded, record or None, expires_at)`. Caching `None` too avoids a Redis read per request for unknown sites. A record is trusted for `PROFILE_MEMORY_SECONDS`. After that, Redis is read again only when `cache.redis_available()`. Otherwise the memory record stays authoritative until `PROFILE_SECONDS` after it was learned, which is tracked as a third tuple item, `expires_at` (monotonic). Without this, a server with no Redis would forget every profile after a minute. The dict is guarded by a `threading.Lock`. When it grows past `MAX_PROFILES`, drop the oldest-loaded half. A Redis record is valid only if `b` is a bool, `r` is a list of str and `c` is a number; anything else counts as no record. Seeds are not stored.

- [ ] **Step 5: Add `profiles.clear_memory()` to `clean_state`** in `tests/conftest.py`.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest -q` and `python -m ruff check .`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scraper/profiles.py scraper/parsers.py tests/test_profiles.py tests/conftest.py
git commit -m "Remember per-site fetch strategies in Redis, seeded from parsers"
```

---

### Task 3: Fetch routes from profiles; drop the 10-minute flag

**Files:**
- Modify: `scraper/fetch.py` (whole `fetch` and `fetch_known_blocked`)
- Modify: `scraper/blocking.py:30-45` (delete `needs_browser`, `remember_needs_browser`, `forget_needs_browser`, `NEEDS_BROWSER_SECONDS`)
- Modify: `scraper/cache.py` (delete the Flags section: `_flags`, `_flags_lock`, `raise_flag`, `flag_is_up`, `lower_flag`; update the module docstring's "flags" line to "small JSON records")
- Modify: `tests/conftest.py` (drop `cache._flags.clear()`)
- Modify: `README.md:14`, `scraper/templates/docs.html:227` (the "remembers for 10 minutes" wording only; Task 5 rewrites the cookie half)
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: `profiles.get_profile`, `profiles.learn_blocked`, `profiles.learn_not_blocked` (Task 2).
- Produces: unchanged `fetch(url, browser="auto", *, owner=None, ready=None, queue_timeout=...) -> Page`. The route order is: `always` → owner cookies that haven't failed (plain) → `get_profile(site).blocks_plain` → `fetch_known_blocked` → plain, then the browser on a block plus `learn_blocked`. `fetch_known_blocked(url, site, in_browser) -> Page` keeps its behaviour but calls `learn_not_blocked` when the plain page is fine. One INFO log line per fetch: `"%s: %s in %.2f s"` (site, route name, seconds), with route names `browser (always)`, `plain+cookies`, `browser (profile)`, `plain`, `plain, then browser`.

- [ ] **Step 1: Update and add tests** in `tests/test_fetch.py`. Replace the `blocking.remember_needs_browser("shop.example")` / `blocking.needs_browser` calls in `test_a_remembered_site_still_gets_a_plain_fetch_when_the_browser_fails` with `profiles.learn_blocked("shop.example")` / `profiles.get_profile("shop.example").blocks_plain`. Add:

```python
IMDB = "https://www.imdb.com/title/tt0111161/"


def test_a_seeded_site_goes_to_the_browser_first(fake):
    assert fetch(IMDB) is RENDERED
    assert fake["http"] == [] and fake["browser"] == 1


def test_cookies_beat_the_profile(fake):
    profiles.learn_blocked("shop.example")
    blocking.save_clearance("alice", "shop.example", [{"name": "aws-waf-token", "value": "x"}], {})
    assert fetch(URL, owner="alice") is OPEN
    assert fake["browser"] == 0 and fake["http"][0] is not None


def test_browser_never_ignores_the_profile(fake):
    profiles.learn_blocked("shop.example")
    fake["http_result"] = BLOCKED
    assert fetch(URL, "never") is BLOCKED
    assert fetch(IMDB, "never") is BLOCKED
    assert fake["browser"] == 0


def test_the_learned_block_outlives_ten_minutes(fake, monkeypatch):
    fake["http_result"] = BLOCKED
    fetch(URL)
    later = time.monotonic() + 11 * 60  # the old needs-browser flag would have expired
    monkeypatch.setattr(profiles.time, "monotonic", lambda: later)
    fetch(URL)
    assert len(fake["http"]) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_fetch.py -q`
Expected: FAIL. `test_a_seeded_site_goes_to_the_browser_first` makes a plain attempt first, and `profiles.learn_blocked` isn't consulted by `fetch`.

- [ ] **Step 3: Switch `fetch.py` to profiles**, add the route and timing log line, and delete the flag functions and helpers listed under Files.

- [ ] **Step 4: Update the README feature bullet and the `docs.html` "Sites that block" row**: a site that blocked plain requests is remembered for 7 days, re-checked about once a day, and known parser sites like IMDb start out marked.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q` and `python -m ruff check .`
Expected: all pass; `grep -rn "needs_browser\|raise_flag\|flag_is_up\|lower_flag" scraper tests` prints nothing.

- [ ] **Step 6: Commit**

```bash
git add scraper/fetch.py scraper/blocking.py scraper/cache.py tests/ README.md scraper/templates/docs.html
git commit -m "Route fetches by learned site profiles instead of a 10-minute flag"
```

---

### Task 4: Render memory for JSON mode, and the daily background re-check

**Files:**
- Modify: `scraper/profiles.py` (add re-check)
- Modify: `scraper/service.py:93-112` (render shortcut and learning)
- Modify: `scraper/fetch.py` (call `maybe_recheck` after the `browser (profile)` route)
- Test: `tests/test_profiles.py`, `tests/test_render_memory.py` (create)

**Interfaces:**
- Consumes: Task 2's profile functions; `http_fetch.fetch_page(url) -> Page`; `blocking.looks_blocked(page) -> bool`; `parsers.parse_page(parser, page)`.
- Produces:
  - `RECHECK_SECONDS = 24 * 3600`; `recheck_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recheck")`.
  - `maybe_recheck(site: str, url: str, parser: Parser | None = None) -> None`. It does nothing for a seeded profile, when `time.time() - checked_at <= RECHECK_SECONDS`, or when that site is already in `rechecks_in_flight: set[str]` (module level, under a lock). Otherwise it first saves the profile with `checked_at=time.time()` (the claim), then submits `run_recheck` to `recheck_pool`. Without a parser it checks `blocks_plain`; with one it checks only `parser.name in render`.
  - `run_recheck(site: str, url: str, parser: Parser | None) -> None`. It calls `fetch_page(url)`. With no parser, a page that isn't `looks_blocked` → `learn_not_blocked(site)`. With a parser, a page that isn't blocked and that `parse_page` reads → `learn_renders_plain(site, parser.name)`. Any `Exception` is logged at INFO and leaves the profile unchanged. The site always leaves the in-flight set (`finally`).

- [ ] **Step 1: Write the failing render tests** in `tests/test_render_memory.py`. Use `QUOTES = "https://quotes.toscrape.com/page/2/"`, a plain page `Page(QUOTES, 200, b"<html></html>")` that fails to parse, and a browser page `Page(QUOTES, 200, <the quote markup from test_cache.PAGE>, "browser")`. Monkeypatch `service.fetch` and `service.fetch_with_browser` to record calls. Stub `profiles.recheck_pool` with an object whose `submit(fn, *args)` records the call without running it.

```python
def test_a_parse_that_needed_the_browser_is_remembered(fakes):
    scrape(QUOTES, "json")
    assert profiles.get_profile("quotes.toscrape.com").render == {"parse_quotes"}
    fakes.reset()
    scrape(QUOTES, "json")
    assert fakes.plain_calls == 0 and fakes.browser_calls == 1


def test_html_mode_is_not_affected_by_render_memory(fakes):
    profiles.learn_needs_render("quotes.toscrape.com", "parse_quotes")
    scrape(QUOTES, "html")
    assert fakes.plain_calls == 1 and fakes.browser_calls == 0


def test_render_memory_respects_browser_never(fakes):
    profiles.learn_needs_render("quotes.toscrape.com", "parse_quotes")
    with pytest.raises(ParseError):
        scrape(QUOTES, "json", "never")
    assert fakes.browser_calls == 0
```

- [ ] **Step 2: Write the failing re-check tests** in `tests/test_profiles.py`. A `sync_pool` fixture monkeypatches `profiles.recheck_pool` with an object whose `submit(fn, *args)` calls `fn(*args)` immediately. Monkeypatch `profiles.fetch_page`.

```python
def test_a_stale_block_is_rechecked_and_cleared(sync_pool, monkeypatch):
    stale("shop.example", blocks_plain=True)  # helper: save_profile(site, SiteProfile(**fields, checked_at=time.time() - 25 * 3600))
    monkeypatch.setattr(profiles, "fetch_page", lambda url: Page(url, 200, b"<html>open</html>"))
    profiles.maybe_recheck("shop.example", "https://shop.example/a")
    assert not profiles.get_profile("shop.example").blocks_plain


def test_a_fresh_profile_is_not_rechecked(sync_pool, monkeypatch):
    profiles.learn_blocked("shop.example")
    monkeypatch.setattr(profiles, "fetch_page", lambda url: pytest.fail("should not fetch"))
    profiles.maybe_recheck("shop.example", "https://shop.example/a")


def test_a_seed_is_never_rechecked(sync_pool, monkeypatch):
    monkeypatch.setattr(profiles, "fetch_page", lambda url: pytest.fail("should not fetch"))
    profiles.maybe_recheck("imdb.com", "https://www.imdb.com/title/tt0111161/")


def test_one_recheck_at_a_time_per_site(monkeypatch):
    submitted = []
    monkeypatch.setattr(profiles, "recheck_pool", SimpleNamespace(submit=lambda fn, *a: submitted.append(a)))
    stale("shop.example", blocks_plain=True)
    profiles.maybe_recheck("shop.example", "https://shop.example/a")
    profiles.maybe_recheck("shop.example", "https://shop.example/b")
    assert len(submitted) == 1


@pytest.mark.parametrize("outcome", [FetchError("down"), UnsupportedContentError("pdf"), RuntimeError("bug"), "blocked"])
def test_a_failed_recheck_keeps_the_route(sync_pool, monkeypatch, outcome):
    stale("shop.example", blocks_plain=True)

    def fetch_page(url):
        if outcome == "blocked":
            return Page(url, 403, b"<title>Just a moment...</title>")
        raise outcome

    monkeypatch.setattr(profiles, "fetch_page", fetch_page)
    profiles.maybe_recheck("shop.example", "https://shop.example/a")
    assert profiles.get_profile("shop.example").blocks_plain
    assert "shop.example" not in profiles.rechecks_in_flight
```

Also add `test_a_stale_render_is_rechecked_and_cleared`, with the quotes parser and a plain page that parses.

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m pytest tests/test_render_memory.py tests/test_profiles.py -q`
Expected: FAIL. `maybe_recheck` doesn't exist, and the second JSON scrape still makes a plain call.

- [ ] **Step 4: Implement the re-check in `profiles.py`** per Interfaces. Import `fetch_page` into the module namespace (`from .http_fetch import fetch_page`) so tests can patch it.

- [ ] **Step 5: Implement the render shortcut in `service.scrape`.** When there is no cached page, and `parser` is set, `browser == "auto"`, `BROWSER_ENABLED` is on and `parser.name in get_profile(site_of(normalized)).render`, fetch with `fetch_with_browser(normalized, owner=owner, ready=ready, queue_timeout=queue_timeout)` inside `gated()`, then call `maybe_recheck(site, normalized, parser)`. In the existing parse-failure branch, after the browser page parses, call `learn_needs_render(site, parser.name)`. In `fetch.py`, call `maybe_recheck(site, url)` after the `browser (profile)` route returns a page.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q` and `python -m ruff check .`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scraper/profiles.py scraper/service.py scraper/fetch.py tests/
git commit -m "Remember parsers that need the browser and re-check stale routes daily"
```

---

### Task 5: Per-key bot-check cookies in Redis

**Files:**
- Modify: `scraper/blocking.py` (Clearance section)
- Modify: `scraper/fetch.py` (the `note_http_result` call)
- Modify: `README.md:14`, `scraper/templates/docs.html:227` (cookie lifetime and storage wording)
- Test: `tests/test_clearances.py` (create)

**Interfaces:**
- Consumes: `cache.get_json`, `cache.set_json` (Task 1).
- Produces:
  - `CLEARANCE_SECONDS = 4 * 3600`. `Clearance.expires` is now wall clock (`time.time()`).
  - `get_clearance(owner, site) -> Clearance | None` checks memory first, then `cache.get_json(f"clearance:{owner}:{site}")`, and fills memory from Redis. It drops cookies whose `expires` is a number greater than 0 and less than or equal to now, and returns `None` if none are left or the record is malformed.
  - `save_clearance(owner, site, cookies, headers) -> None` keeps its signature and writes to memory and Redis with `ttl = CLEARANCE_SECONDS`. The value is `{"cookies": [...], "headers": {...}, "expires": float, "http_works": bool | None}`.
  - **Changed signature:** `note_http_result(owner: str, site: str, clearance: Clearance, worked: bool) -> None` also rewrites the Redis record, with the TTL being the time left until `clearance.expires`.

- [ ] **Step 1: Write the failing tests** in `tests/test_clearances.py`

```python
COOKIE = {"name": "aws-waf-token", "value": "x", "domain": ".shop.example", "path": "/", "expires": -1}


def test_cookies_survive_a_restart_through_redis(redis):
    blocking.save_clearance("alice", "shop.example", [COOKIE], {"user-agent": "UA"})
    blocking.clearances.clear()
    found = blocking.get_clearance("alice", "shop.example")
    assert found.cookies == [COOKIE] and found.headers == {"user-agent": "UA"}
    assert blocking.get_clearance("bob", "shop.example") is None


def test_cookies_live_four_hours(redis):
    blocking.save_clearance("alice", "shop.example", [COOKIE], {})
    assert 4 * 3600 - 5 < cache.redis_client.ttl("scrape:v2:clearance:alice:shop.example") <= 4 * 3600


def test_cookies_past_their_own_expiry_are_dropped(redis):
    old = {**COOKIE, "name": "old", "expires": time.time() - 1}
    blocking.save_clearance("alice", "shop.example", [COOKIE, old], {})
    blocking.clearances.clear()
    assert [c["name"] for c in blocking.get_clearance("alice", "shop.example").cookies] == ["aws-waf-token"]


def test_a_failed_plain_attempt_is_remembered_across_restarts(redis):
    blocking.save_clearance("alice", "shop.example", [COOKIE], {})
    blocking.note_http_result("alice", "shop.example", blocking.get_clearance("alice", "shop.example"), False)
    blocking.clearances.clear()
    assert blocking.get_clearance("alice", "shop.example").http_works is False


def test_a_malformed_record_reads_as_none(redis):
    cache.set_json("clearance:alice:shop.example", {"cookies": "nope"}, 600)
    assert blocking.get_clearance("alice", "shop.example") is None


def test_cookies_work_from_memory_without_redis():
    blocking.save_clearance("alice", "shop.example", [COOKIE], {})
    assert blocking.get_clearance("alice", "shop.example") is not None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_clearances.py -q`
Expected: FAIL. Nothing reaches Redis, and `note_http_result` takes 2 arguments.

- [ ] **Step 3: Implement the write-through in `blocking.py`**, switch expiry to `time.time()`, and update the `note_http_result` call in `fetch.py` to pass `owner, site`. Keep the `MAX_CLEARANCES` pruning for memory.

- [ ] **Step 4: Update the README and `docs.html` cookie wording**: cookies are kept per key for up to 4 hours, in Redis when it's configured, so they survive restarts.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q` and `python -m ruff check .`
Expected: all pass, including `test_saved_bot_check_cookies_are_only_used_by_their_owner`.

- [ ] **Step 6: Commit**

```bash
git add scraper/blocking.py scraper/fetch.py tests/test_clearances.py README.md scraper/templates/docs.html
git commit -m "Keep per-key bot-check cookies in Redis for up to four hours"
```

---

### Task 6: Warm browsers at startup

**Files:**
- Modify: `scraper/browser.py` (constants and two new functions after `get_browser`)
- Modify: `scraper/app.py:33-38` (lifespan)
- Modify: `tests/conftest.py` (`os.environ["BROWSER_WARM"] = "off"` before imports)
- Modify: `README.md` (feature bullet; `BROWSER_WARM=off` in "Run it")
- Test: `tests/test_warm.py` (create)

**Interfaces:**
- Produces:
  - `BROWSER_WARM = os.environ.get("BROWSER_WARM", "on").lower() != "off"`; `WARM_BARRIER_SECONDS = 30`.
  - `warm_browsers() -> int` submits `BROWSER_WORKERS` tasks to `browser_pool`. Each task does `barrier.wait()` on a `threading.Barrier(BROWSER_WORKERS, timeout=WARM_BARRIER_SECONDS)`, then `get_browser()`. It waits for all of them, logs and swallows each task's exception (including `threading.BrokenBarrierError`), and returns how many started.
  - `start_warming() -> threading.Thread | None` returns `None` unless `BROWSER_ENABLED and BROWSER_WARM`. Otherwise it starts and returns a daemon thread named `"browser-warmup"` that runs `warm_browsers`.
  - `app.py`: an `asynccontextmanager` lifespan that calls `start_warming()` and then `yield`s, passed as `FastAPI(..., lifespan=lifespan)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_warm.py`

```python
def test_each_browser_worker_starts_its_own_browser(monkeypatch):
    threads = []
    monkeypatch.setattr(browser, "get_browser", lambda: threads.append(threading.current_thread().name))
    assert browser.warm_browsers() == browser.BROWSER_WORKERS
    assert len(set(threads)) == browser.BROWSER_WORKERS


def test_a_browser_that_cannot_start_does_not_stop_the_server(monkeypatch):
    def broken():
        raise FetchError("The browser could not start: executable doesn't exist")

    monkeypatch.setattr(browser, "get_browser", broken)
    assert browser.warm_browsers() == 0


def test_warming_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(browser, "BROWSER_WARM", False)
    assert browser.start_warming() is None


def test_the_app_starts_warming_on_startup(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "start_warming", lambda: calls.append(1))
    with TestClient(app_module.app):
        pass
    assert calls == [1]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_warm.py -q`
Expected: FAIL with `AttributeError: module 'scraper.browser' has no attribute 'warm_browsers'`

- [ ] **Step 3: Implement `warm_browsers`, `start_warming` and the lifespan** per Interfaces. Import `start_warming` into `app.py` by name so the test can patch `app_module.start_warming`.

- [ ] **Step 4: Set `BROWSER_WARM=off` in `tests/conftest.py`** next to the other environment settings. Add a README line that warm browsers start in the background at boot and that `BROWSER_WARM=off` disables it.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q` and `python -m ruff check .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add scraper/browser.py scraper/app.py tests/test_warm.py tests/conftest.py README.md
git commit -m "Start browser workers in the background at boot"
```

---

### Task 7: Local end-to-end check (no proxy)

**Files:** none (verification only)

- [ ] **Step 1: Start the server locally without a proxy**, from PowerShell: `Remove-Item Env:PROXY_URL -ErrorAction SilentlyContinue; $env:API_KEYS = "k1,k2"; uvicorn main:app`
Expected in the log within a few seconds, before any request: two `Starting a browser in browser_N` lines.

- [ ] **Step 2: First IMDb request**, from another PowerShell terminal (`curl.exe`, because plain `curl` is an alias for `Invoke-WebRequest` in Windows PowerShell 5.1): `curl.exe -s -o NUL -w "%{time_total}\n" -H "X-API-Key: k1" "http://127.0.0.1:8000/scrape?url=https://www.imdb.com/title/tt0111161/&mode=json"`
Expected: a log line `imdb.com: browser (profile) ...` with no `looks blocked` line before it, since the seed skips the plain attempt. Total time about 3 s.

- [ ] **Step 3: The same key again with a different title** (`tt0068646`)
Expected: `imdb.com: plain+cookies ...`, about 1 s.

- [ ] **Step 4: Key `k2`, a third title** (`tt0071562`)
Expected: `browser (profile)` with no plain attempt.

- [ ] **Step 5 (only with Redis 6 or newer running locally, e.g. `docker run -p 6379:6379 redis:7`):** restart the server with `REDIS_URL` set and repeat Step 3 for `k1` with a fourth title (`tt0468569`).
Expected: `plain+cookies`, since the cookies came back from Redis after the restart. Your current local Redis rejects `HELLO`; skip this step if it's still the one running.

- [ ] **Step 6: Report** the measured times next to the spec's baseline table (6.9 s first fetch, 1.0 s with cookies).
