"""Who is asking: the API key check, and the rate limits charged before anything is fetched."""

import hashlib
import logging
import secrets
import time

from fastapi import HTTPException, Request
from fastapi.security import APIKeyHeader
from limits import RateLimitItem

from ..settings import settings
from ..store import quota

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)

logger = logging.getLogger(__name__)
if not settings.api_keys:
    logger.warning("API_KEYS is empty, every request will be rejected.")


def is_valid_key(key: str | None, keys: list[str]) -> bool:
    # Compare with every key (a list, not a short-circuiting generator), so timing doesn't tell which matched.
    return bool(key) and any([secrets.compare_digest(key.encode(), k.encode()) for k in keys])


def key_id(api_key: str) -> str:
    """A stable, non-reversible name for a key, for quotas and per-key cookies."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def client_ip(request: Request) -> str:
    forwarded = request.headers.get(settings.trusted_ip_header) if settings.trusted_ip_header else None
    return forwarded or (request.client.host if request.client else "unknown")


async def charge(limits: list[RateLimitItem], *ids: str, cost: int = 1) -> None:
    exceeded = await quota.take(limits, *ids, cost=cost)
    if exceeded:
        limit, reset_at = exceeded
        retry_after = max(1, int(reset_at - time.time()) + 1)
        raise HTTPException(429, f"Rate limit exceeded: {limit}", headers={"Retry-After": str(retry_after)})


async def authorize(request: Request, api_key: str | None, cost: int = 1) -> str:
    """The owner id of a valid key. A missing or wrong key is charged to the caller's IP, so guessing stays slow."""
    if not is_valid_key(api_key, settings.api_keys):
        await charge(quota.IP_LIMITS, "ip", client_ip(request))
        raise HTTPException(401, "Invalid or missing API key")
    owner = key_id(api_key)
    await charge(quota.KEY_LIMITS, "key", owner, cost=cost)
    return owner


async def authorize_admin(request: Request, admin_key: str | None) -> None:
    """Changing site strategies needs a key from ADMIN_KEYS; a wrong one is charged to the IP like a wrong API key."""
    if not is_valid_key(admin_key, settings.admin_keys):
        await charge(quota.IP_LIMITS, "ip", client_ip(request))
        raise HTTPException(401, "Invalid or missing admin key")
