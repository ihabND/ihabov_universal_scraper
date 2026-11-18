"""Recognising bot protection. A new vendor is one more entry in CHALLENGES."""

BLOCKED_STATUS_CODES = {202, 403, 429, 503}
CHALLENGE_PAGE_MAX_BYTES = 150 * 1024  # bot-check pages are small; a real page may mention a vendor anywhere

CHALLENGES = {
    "aws-waf": (b"awswaf",),
    "cloudflare": (b"_cf_chl_opt", b"<title>Just a moment...</title>"),
    "datadome": (b"captcha-delivery.com",),
}


def challenge_of(content: bytes) -> str | None:
    """The vendor whose bot check this page is, or None."""
    if len(content) > CHALLENGE_PAGE_MAX_BYTES:
        return None
    return next((vendor for vendor, markers in CHALLENGES.items() if any(m in content for m in markers)), None)


def looks_blocked(status_code: int, content: bytes) -> bool:
    return status_code in BLOCKED_STATUS_CODES or challenge_of(content) is not None
