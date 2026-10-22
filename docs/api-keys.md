# Accounts and API keys

Every scrape needs an account. People sign up on the website, then create API keys on their dashboard. Nothing is handed out by hand any more, and the old `API_KEYS` environment variable is gone.

## How it works

- **Accounts** (`scraper/users.py`): sign-up with email and password at `/signup`, login at `/login`. Passwords are hashed with scrypt from the standard library. There is no email verification and no password reset yet.
- **Sessions:** logging in sets a `session` cookie (`HttpOnly`, `SameSite=Lax`, `Secure` over HTTPS) that lasts 30 days. The database stores only the SHA-256 of the cookie value.
- **API keys** (`scraper/auth.py`): every account can have up to **3** keys. A key looks like `sk_…` (32 random bytes). It's shown once, when it's created. The database stores only its SHA-256 and the first 9 characters, for the dashboard. Revoking deletes the key, and it stops working right away.
- **Storage** (`scraper/db.py`): one SQLite file at `DATABASE_PATH` (default `data/scraper.db`). The tables are created on first use.
- **Forms are protected against cross-site requests:** every form that changes something carries the session's CSRF token, and form posts whose `Origin` header names another site are refused.

## Pages

| Route | What it does |
|---|---|
| `/` | The landing page: what the scraper does, supported sites, the free account, links to sign up and log in. Logged-in users are sent to `/app`. Old links like `/?url=…&mode=json` are sent to `/app?url=…&mode=json`. |
| `/signup`, `/login` | Create an account (then lands on `/app`), log in (then back to the page that asked for it, or `/app`). `POST /logout` logs out. |
| `/dashboard` | Usage (left right now, today, total, the last 7 days) and the API keys: create, see when each was last used, revoke. |
| `/account` | Change the password (logs out every other device), log out, delete the account (needs the password; deletes the keys and usage too). |
| `/app` | The workbench, for logged-in users only (others are sent to log in and come back with the same URL). Scraping there uses the login: the page sends the session cookie plus the CSRF token in `X-CSRF-Token`. |

## Limits

All limits live in `scraper/quota.py`, use a moving window, and are shared across machines through Redis when `REDIS_URL` is set.

| Limit | Default | Env variable |
|---|---|---|
| Scrapes per user, all keys and the website together, one per URL | 10 a minute | `USER_RATE_LIMIT` |
| Requests with a missing or wrong key, per IP | 10 a minute | `IP_RATE_LIMIT` |
| Login attempts, per IP and per email (password checks on `/account` count too) | 5 a minute | `LOGIN_RATE_LIMIT` |
| Sign-ups per IP | 3 a day | `SIGNUP_RATE_LIMIT` |

Values use the `limits` syntax, e.g. `USER_RATE_LIMIT="10/minute"` or `"10/minute;500/day"`.

A successful `/scrape` or `/scrape/batch` response carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` (seconds). Over the limit, the answer is `429` with `Retry-After`.

## Using the API

Send the key in the `X-API-Key` header. Never put it in the URL: query strings end up in logs and browser history.

```powershell
curl.exe -H "X-API-Key: YOUR_KEY" "https://ihabov-universal-scraper.fly.dev/scrape?url=https://example.com"
```

Python:

```python
import os
import requests

resp = requests.get(
    "https://ihabov-universal-scraper.fly.dev/scrape",
    params={"url": "https://example.com", "mode": "html"},  # mode: "html" or "json"
    headers={"X-API-Key": os.environ["SCRAPER_API_KEY"]},
    timeout=120,  # a slow site can use the 25-second fetch budget and then a browser run
)
resp.raise_for_status()
print(resp.json()["content"])
```

A missing, wrong or revoked key returns:

```json
HTTP 401
{"detail": "Invalid or missing API key"}
```

## Deploying on Fly.io

The database lives on a Fly volume, so accounts survive restarts and deploys. `fly.toml` mounts it at `/data` and sets `DATABASE_PATH=/data/scraper.db`. Once, before the first deploy with accounts:

```powershell
fly volumes create scraper_data -r ams -s 1 -a ihabov-universal-scraper
fly secrets unset API_KEYS -a ihabov-universal-scraper   # no longer read
```

A volume belongs to one machine, so the app runs on one machine. Running several would mean moving the database to Postgres.

Backup, by making a consistent copy on the machine and downloading it (copying `scraper.db` directly can miss the latest writes, which SQLite keeps in `scraper.db-wal` for a while):

```powershell
fly ssh console -a ihabov-universal-scraper -C "/app/.venv/bin/python -c ""import sqlite3; sqlite3.connect('/data/scraper.db').backup(sqlite3.connect('/data/backup.db'))"""
fly ssh sftp get /data/backup.db scraper-backup.db -a ihabov-universal-scraper
```

## Running locally

```powershell
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/signup, create an account, and create a key on the dashboard. The database is created at `data/scraper.db` (ignored by git). Delete that file to start over.
