import logging
import os
import secrets

from fastapi.security import APIKeyHeader

logger = logging.getLogger(__name__)

# Comma-separated list of valid keys, e.g. API_KEYS="key1,key2".
# On Fly.io this is set with `fly secrets set API_KEYS=...`.
API_KEYS = [k.strip() for k in os.environ.get("API_KEYS", "").split(",") if k.strip()]
if not API_KEYS:
    logger.warning("API_KEYS is empty, every request will be rejected.")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def is_valid_key(key: str | None) -> bool:
    if not key:
        return False
    # Check every key with compare_digest so timing doesn't leak which key matched.
    matches = [secrets.compare_digest(key.encode(), k.encode()) for k in API_KEYS]
    return any(matches)
