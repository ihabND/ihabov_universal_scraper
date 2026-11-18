"""Every setting that can be chosen for scraping a site. A site's strategy changes some of them; the rest keep
these defaults."""

from dataclasses import asdict, dataclass, field

PROXY_MODES = ("default", "direct", "proxy")  # default follows PROXY_BYPASS
START_TIERS = ("http", "http-safari", "browser")
FINGERPRINTS = ("chrome", "safari")
PLAIN_TIER = {"chrome": "http", "safari": "http-safari"}  # the fetcher of each fingerprint


@dataclass
class Strategy:
    start: str = "http"  # the fetcher to start with: http, http-safari or browser
    render: list[str] = field(default_factory=list)  # page kinds that need JavaScript: parser names, or "html"
    fingerprints: list[str] = field(default_factory=lambda: ["chrome", "safari"])  # plain-HTTP fingerprints, in order
    proxy: str = "default"
    retries: int = 2
    request_timeout: float = 10.0
    fetch_budget: float = 25.0
    load_timeout: float = 30.0
    challenge_wait: float = 20.0
    render_wait: float = 5.0
    skip_resources: list[str] = field(default_factory=lambda: ["image", "font", "media"])
    locale: str = "en-US"
    headers: dict[str, str] = field(default_factory=dict)
    url_params: dict[str, str] = field(default_factory=dict)
    batch_at_once: int = 4
    batch_gap: float = 0.25
    cache_seconds: int = 600
    failed_cache_seconds: int = 30


DEFAULTS = asdict(Strategy())


def effective(pinned: dict, learned: dict[str, dict], seed_start: str) -> Strategy:
    """Pinned values win over learned ones, which win over the defaults. Plain HTTP starts with the site's first
    fingerprint unless a start was pinned or learned."""
    values = {**DEFAULTS, "start": seed_start}
    values.update({name: entry["value"] for name, entry in learned.items() if name in DEFAULTS})
    values.update({name: value for name, value in pinned.items() if name in DEFAULTS})
    if values["start"] == "http" and "start" not in pinned and "start" not in learned:
        values["start"] = PLAIN_TIER[values["fingerprints"][0]]
    return Strategy(**values)


def check_pins(pins: dict) -> dict:
    """The pins, when every name is a setting and every value has that setting's type; otherwise ValueError."""
    for name, value in pins.items():
        if name not in DEFAULTS:
            raise ValueError(f"Unknown setting: {name}")
        default = DEFAULTS[name]
        if isinstance(default, float):
            valid = isinstance(value, int | float) and not isinstance(value, bool)
        else:
            valid = type(value) is type(default)
        if valid and isinstance(value, list):
            valid = all(isinstance(item, str) for item in value)
        if valid and isinstance(value, dict):
            valid = all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
        if not valid:
            raise ValueError(f"{name} must be a {type(default).__name__}, like {default!r}")
        if isinstance(value, int | float) and value < (1 if name == "batch_at_once" else 0):
            raise ValueError(f"{name} can't be {value}")
        for allowed, setting in ((PROXY_MODES, "proxy"), (START_TIERS, "start")):
            if name == setting and value not in allowed:
                raise ValueError(f"{name} must be one of {', '.join(allowed)}")
        if name == "fingerprints" and (not value or not set(value) <= set(FINGERPRINTS)):
            raise ValueError(f"fingerprints must list some of {', '.join(FINGERPRINTS)}")
    return pins
