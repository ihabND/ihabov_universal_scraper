"""Every setting that can be chosen for scraping a site. A site's strategy changes some of them; the rest keep
these defaults."""

from dataclasses import asdict, dataclass, field

PROXY_MODES = ("default", "direct", "proxy")  # default follows PROXY_BYPASS


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
    """Pinned values win over learned ones, which win over the defaults."""
    values = {**DEFAULTS, "start": seed_start}
    values.update({name: entry["value"] for name, entry in learned.items() if name in DEFAULTS})
    values.update({name: value for name, value in pinned.items() if name in DEFAULTS})
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
        if not valid:
            raise ValueError(f"{name} must be a {type(default).__name__}, like {default!r}")
        if name == "proxy" and value not in PROXY_MODES:
            raise ValueError(f"proxy must be one of {', '.join(PROXY_MODES)}")
    return pins
