# API keys

Every scrape needs a valid API key. Keys are handed out by hand: there is no sign-up. Each person gets their own key, so you can take access away from one person without affecting anyone else.

## How it works

- Valid keys are read from the `API_KEYS` environment variable, as a comma-separated list: `API_KEYS="key1,key2,key3"`.
- On Fly.io, `API_KEYS` is a **secret**. It's encrypted, never in git, and passed to the app as an environment variable.
- The check lives in `auth.py`. Keys are compared with `secrets.compare_digest`, so response timing doesn't reveal anything about a key.
- **The app fails closed:** if `API_KEYS` is empty or missing, every request is rejected with `401`.
- The keys are loaded once when the app starts. After changing them, the app has to restart. `fly secrets set` does that automatically.

## What is protected

| Route         | Access                                                                 |
|---------------|------------------------------------------------------------------------|
| `GET /`       | Public. Shows the web form.                                            |
| `POST /`      | The web form. Needs a valid key in the form's "API key" field.         |
| `GET /scrape` | JSON API. Needs a valid key in the `X-API-Key` header.                 |
| `/docs`, `/redoc`, `/openapi.json` | Disabled (404).                                   |

## Generating a key

Make one key per person:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

This gives 32 random bytes, about 43 URL-safe characters. Don't make keys up by hand.

Keep a private note of which key belongs to whom, for example in a password manager. You'll need it to revoke the right one.

## Adding, listing, and revoking keys on Fly.io

`API_KEYS` always holds the **complete** list, so every change means setting the whole list again.

Add a key (include all existing keys plus the new one):

```powershell
fly secrets set API_KEYS="existing_key_1,existing_key_2,new_key" -a ihabov-universal-scraper
```

Revoke a key (set the list again without it):

```powershell
fly secrets set API_KEYS="existing_key_1" -a ihabov-universal-scraper
```

See whether the secret is set (Fly only shows a digest, never the value):

```powershell
fly secrets list -a ihabov-universal-scraper
```

Setting a secret restarts the app with the new keys within a few seconds.

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
    params={"url": "https://example.com", "mode": "text"},  # mode: "text" or "html"
    headers={"X-API-Key": os.environ["SCRAPER_API_KEY"]},
    timeout=30,
)
resp.raise_for_status()
print(resp.json()["content"])
```

A missing or wrong key returns:

```json
HTTP 401
{"detail": "Invalid or missing API key"}
```

## Using the website

Open https://ihabov-universal-scraper.fly.dev/, enter the URL, choose text or HTML, paste your key, and press **Scrape**.

## Running locally

Set the variable before starting the app:

```powershell
$env:API_KEYS = "devkey"
uvicorn main:app --reload
```

Then use `devkey` as the key at http://127.0.0.1:8000/.
