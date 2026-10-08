# Improvements

Next steps to make this project stronger as a portfolio piece, ranked by payoff per hour.

## Where it stands (October 2026)

The engineering underneath is solid: TLS-impersonated fetching with a headless-browser fallback, SSRF protection with redirect-hop validation, rate limiting, Redis caching, fetch budgets and streaming size caps, nine site parsers, a designed front end, Docker and a Fly.io deploy.

What a reviewer sees on the repo page is different: no README, no tests, no CI, and the two largest files are a 1,900-line learning guide and a key-management note. The repo reads as a course, not as a product.

## 1. Highest payoff first

### README with screenshots
- One paragraph on what it does.
- Two screenshots: the front page with a JSON result, and the docs page.
- An architecture sketch of the fetch pipeline: HTTP, budget, blocked check, browser, hop validation, cache.
- A curl example and a "run it locally" block.
- Move `next_steps_guide.md` into a `docs/` folder or out of the repo.

### Tests, with CI running them
- Parser tests against saved HTML fixtures, no network.
- Unit tests for URL normalisation, host blocking, the redirect-hop check, the fetch budget and the cache key.
- A GitHub Actions workflow running `pytest` and `ruff` on every push, with the badge in the README.
- Most of these checks were already written ad hoc during development; they need to live in `tests/`.

### Project hygiene
- Pinned dependencies: `pyproject.toml` with `uv` or a lock file.
- `ruff` and `mypy` configured.
- A `LICENSE`.
- A `/health` endpoint.
- Split the app into a package (`app/fetch.py`, `app/browser.py`, `app/parsers/`) instead of four flat modules.
- Re-enable the generated OpenAPI docs at a non-default path. Hiding Swagger reads as a missing feature, not as security.

## 2. Features that make it a story

### Generic structured extraction for any site
JSON mode only works on nine hand-picked sites. A fallback that returns JSON-LD, OpenGraph and meta tags for any page makes the "universal" in the name true. `find_json_ld` already exists, so this is a short job.

### Schema-driven extraction with an LLM
Let the caller pass a JSON schema or field list and have a model fill it from the page text. Shows responsible API integration: token budgets, caching by content hash, cost caps. Removes the per-site parser bottleneck.

### Async job model for slow scrapes
A browser run can take a minute. `POST /jobs` returns an id; the caller polls or gets a webhook callback. The standard pattern for scraping APIs and a good systems-design talking point. Pairs with per-key quotas and a usage endpoint.

### Observability
Structured JSON logs with a request id, timing per stage (http, browser, parse), and a `/metrics` endpoint. Cheap to add, and it allows real numbers in the README such as median fetch time per path.

## 3. Smaller polish

- Respect `robots.txt` with an opt-out flag, and say so in the docs. Signals thought about the ethics of scraping.
- A small Python client package on PyPI with the three-line example from the docs.
- Keep commit messages descriptive going forward.
- A security pass on the deployed headers: HSTS, CSP, no-sniff. The Fly proxy handles TLS; the app sets nothing else.

## One-weekend plan

1. Saturday morning: README and screenshots.
2. Saturday afternoon and evening: tests plus CI.
3. Sunday: generic JSON-LD and OpenGraph extraction.

Result: the repo goes from "followed a guide" to "built and shipped a scraping API with a test suite."
