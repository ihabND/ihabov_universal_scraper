"""The permanent strategy of every scraped site, in a SQLite file, read and edited through the Sites page.

One row per site: values pinned by hand, values the scraper learned (with when and why), flags where the
two disagree, and the site's last use. The rows are tiny, so the calls run on the event loop. A failing
database is logged and never fails a scrape.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..core.strategy import Strategy, effective
from ..net.urls import domain_of
from ..settings import settings

CHECK_SECONDS = 24 * 3600
SCHEMA = """CREATE TABLE IF NOT EXISTS sites (
    site TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    pinned TEXT NOT NULL DEFAULT '{}',
    learned TEXT NOT NULL DEFAULT '{}',
    flags TEXT NOT NULL DEFAULT '{}',
    checked_at REAL NOT NULL DEFAULT 0,
    last TEXT NOT NULL DEFAULT '{}',
    requests INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0
)"""

logger = logging.getLogger(__name__)
db: sqlite3.Connection | None = None


@dataclass
class SiteRecord:
    site: str
    domain: str
    pinned: dict
    learned: dict[str, dict]  # setting -> {"value", "at", "evidence"}
    flags: dict[str, dict]  # setting -> {"at", "seen"}
    checked_at: float
    last: dict  # {"at", "route", "status", "seconds"}
    requests: int
    ok: int


def execute(sql: str, args: tuple = ()) -> list[tuple]:
    global db
    try:
        if db is None:
            Path(settings.strategy_db).parent.mkdir(parents=True, exist_ok=True)
            # Autocommit; the server, its worker threads and the test client may all touch it.
            db = sqlite3.connect(settings.strategy_db, isolation_level=None, check_same_thread=False)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(SCHEMA)
        return db.execute(sql, args).fetchall()
    except (sqlite3.Error, OSError) as e:
        logger.warning("Strategy store failed: %s", e)
        return []


def close() -> None:
    global db
    if db is not None:
        db.close()
        db = None


def now_text() -> str:
    return datetime.now().isoformat(sep=" ", timespec="minutes")


def record(row: tuple) -> SiteRecord:
    site, domain, pinned, learned, flags, checked_at, last, requests, ok = row
    return SiteRecord(
        site,
        domain,
        json.loads(pinned),
        json.loads(learned),
        json.loads(flags),
        checked_at,
        json.loads(last),
        requests,
        ok,
    )


def get(site: str) -> SiteRecord | None:
    rows = execute("SELECT * FROM sites WHERE site = ?", (site,))
    return record(rows[0]) if rows else None


def all_records() -> list[SiteRecord]:
    return [record(row) for row in execute("SELECT * FROM sites ORDER BY domain, site")]


def strategy_for(site: str, seed_start: str) -> Strategy:
    found = get(site)
    return effective(found.pinned if found else {}, found.learned if found else {}, seed_start)


def ensure(site: str) -> SiteRecord:
    execute("INSERT INTO sites (site, domain) VALUES (?, ?) ON CONFLICT(site) DO NOTHING", (site, domain_of(site)))
    return get(site) or SiteRecord(site, domain_of(site), {}, {}, {}, 0.0, {}, 0, 0)


def learn(site: str, setting: str, value, evidence: str) -> None:
    """Keep what the scraper found, with when and why. A pin that disagrees stays, with a flag."""
    found = ensure(site)
    found.learned[setting] = {"value": value, "at": now_text(), "evidence": evidence}
    if setting in found.pinned and found.pinned[setting] != value:
        found.flags[setting] = {"at": now_text(), "seen": evidence}
    execute(
        "UPDATE sites SET learned = ?, flags = ?, checked_at = ? WHERE site = ?",
        (json.dumps(found.learned), json.dumps(found.flags), time.time(), site),
    )


def record_use(site: str, route: str, status: int, seconds: float, ok: bool) -> None:
    last = json.dumps({"at": now_text(), "route": route, "status": status, "seconds": round(seconds, 2)})
    execute(
        "INSERT INTO sites (site, domain, last, requests, ok) VALUES (?, ?, ?, 1, ?) ON CONFLICT(site) DO UPDATE "
        "SET last = excluded.last, requests = requests + 1, ok = ok + excluded.ok",
        (site, domain_of(site), last, int(ok)),
    )


def claim_check(site: str, every: float = CHECK_SECONDS) -> bool:
    """True for the one caller that gets to run the site's check now; the claim lasts `every` seconds."""
    now = time.time()
    return bool(
        execute(
            "UPDATE sites SET checked_at = ? WHERE site = ? AND checked_at < ? RETURNING site", (now, site, now - every)
        )
    )


def set_pins(site: str, pins: dict) -> None:
    ensure(site)
    execute("UPDATE sites SET pinned = ? WHERE site = ?", (json.dumps(pins), site))


def clear_flags(site: str) -> None:
    execute("UPDATE sites SET flags = '{}' WHERE site = ?", (site,))


def export_pins() -> dict[str, dict]:
    return {r.site: r.pinned for r in all_records() if r.pinned}


def import_pins(data: dict[str, dict]) -> int:
    for site, pins in data.items():
        set_pins(site, pins)
    return len(data)
