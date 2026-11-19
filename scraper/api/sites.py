"""Every site's strategy: reading it needs an API key, changing it an admin key."""

from dataclasses import asdict

from fastapi import APIRouter, Body, HTTPException, Request, Security

from ..core.strategy import DEFAULTS, check_pins, effective
from ..net.urls import site_of
from ..parsers import seed_tier
from ..store import strategies
from ..store.strategies import SiteRecord
from .deps import admin_key_header, api_key_header, authorize, authorize_admin

router = APIRouter(prefix="/api/sites")


def describe(record: SiteRecord) -> dict:
    """Each setting's value in force and where it comes from: pinned, learned, the parser's guess, the first
    fingerprint, or default."""
    seed = seed_tier(record.site)
    in_force = asdict(effective(record.pinned, record.learned, seed))
    rows = []
    for name, default in DEFAULTS.items():
        learned = record.learned.get(name)
        if name in record.pinned:
            source, at = "pinned", record.pinned_at.get(name)
        elif learned:
            source, at = "learned", learned["at"]
        elif in_force[name] != default:  # only start has a default of its own
            source, at = "parser" if seed != default else "fingerprints", None
        else:
            source, at = "default", None
        rows.append(
            {"name": name, "value": in_force[name], "source": source, "at": at, "default": default, "learned": learned}
        )
    return {
        "site": record.site,
        "settings": rows,
        "pins": record.pinned,
        "flags": record.flags,
        "last": record.last,
        "requests": record.requests,
        "ok": record.ok,
    }


def checked(site: str, pins: dict) -> None:
    if not site or site_of(f"https://{site}/") != site:
        raise HTTPException(400, f"Not a site name: {site}")
    try:
        check_pins(pins)
    except ValueError as e:
        raise HTTPException(400, f"{site}: {e}") from None


@router.get("")
async def list_sites(request: Request, api_key: str | None = Security(api_key_header)) -> list[dict]:
    """Sites grouped by domain, sorted by name."""
    await authorize(request, api_key)
    groups: dict[str, list] = {}
    for record in strategies.all_records():
        groups.setdefault(record.domain, []).append(describe(record))
    return [{"domain": domain, "sites": sites} for domain, sites in groups.items()]


@router.get("/export")
async def export_sites(request: Request, admin_key: str | None = Security(admin_key_header)) -> dict[str, dict]:
    """Every site's whole record."""
    await authorize_admin(request, admin_key)
    return strategies.export_all()


@router.post("/import")
async def import_pins(
    request: Request, data: dict[str, dict] = Body(...), admin_key: str | None = Security(admin_key_header)
) -> dict:
    """Sets the pins of every site in an exported file (only its "pinned" values count), or of none when any of
    them is wrong."""
    await authorize_admin(request, admin_key)
    pins = {}
    for site, record in data.items():
        if not isinstance(record.get("pinned"), dict):
            raise HTTPException(400, f'{site}: no "pinned" settings. Import a file made by Export.')
        checked(site, record["pinned"])
        pins[site] = record["pinned"]
    return {"imported": strategies.import_pins(pins)}


@router.put("/{site}/pins")
async def set_pins(
    request: Request, site: str, pins: dict = Body(..., embed=True), admin_key: str | None = Security(admin_key_header)
) -> dict:
    """Replaces the site's pins: a setting left out is unpinned and goes back to learned or default."""
    await authorize_admin(request, admin_key)
    checked(site, pins)
    strategies.set_pins(site, pins)
    return {"site": site, "pins": pins}


@router.delete("/{site}/flags")
async def clear_flags(request: Request, site: str, admin_key: str | None = Security(admin_key_header)) -> dict:
    await authorize_admin(request, admin_key)
    strategies.clear_flags(site)
    return {"site": site}


@router.delete("/{site}")
async def delete_site(request: Request, site: str, admin_key: str | None = Security(admin_key_header)) -> dict:
    """Forgets the site: its pins, what was learned, its flags and counts. A later scrape starts it afresh."""
    await authorize_admin(request, admin_key)
    strategies.delete(site)
    return {"site": site}
