"""Static Data Export download and import.

CCP publishes the SDE as a zip of JSON Lines files, one per table, keyed by a
build number. We store that build number and only re-download when it moves,
so "update the game data" is a cheap check rather than a 95 MB download.

Only the tables the asset browser needs get imported. mapMoons and friends are
skipped -- station names are derived from the fields on the station record
itself, per CCP's celestial naming rules.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import db
from .config import CACHE_DIR, SDE_BUILD_URL, SDE_LATEST_URL, Settings, user_agent

Progress = Callable[[str, int], None]  # (message, percent 0-100)

_ROMAN = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]


def roman(n: int) -> str:
    out = []
    for value, sym in _ROMAN:
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


def _en(record: dict, key: str = "name") -> str:
    """SDE name fields are localisation dicts; we only keep English."""
    val = record.get(key)
    if isinstance(val, dict):
        return val.get("en") or next(iter(val.values()), "")
    return val or ""


def latest_build(settings: Settings | None = None) -> int:
    r = httpx.get(SDE_LATEST_URL, headers={"User-Agent": user_agent(settings)}, timeout=30)
    r.raise_for_status()
    for line in r.text.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("_key") == "sde":
            return int(rec["buildNumber"])
    raise RuntimeError("no 'sde' record in latest.jsonl")


def installed_build(conn: sqlite3.Connection) -> int | None:
    val = db.get_meta(conn, "sde_build")
    return int(val) if val else None


def download(build: int, settings: Settings | None = None, progress: Progress | None = None) -> Path:
    dest = CACHE_DIR / f"sde-{build}.zip"
    if dest.exists() and dest.stat().st_size > 1_000_000:
        return dest
    tmp = dest.with_suffix(".part")
    url = SDE_BUILD_URL.format(build=build)
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=120,
        headers={"User-Agent": user_agent(settings)},
    ) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes(1 << 18):
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(f"Downloading SDE {build}", int(done * 100 / total))
    tmp.replace(dest)
    for old in CACHE_DIR.glob("sde-*.zip"):
        if old != dest:
            old.unlink(missing_ok=True)
    return dest


def _records(zf: zipfile.ZipFile, name: str) -> Iterator[dict]:
    with zf.open(name) as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def import_zip(
    conn: sqlite3.Connection, zip_path: Path, build: int, progress: Progress | None = None
) -> None:
    zf = zipfile.ZipFile(zip_path)
    steps = [
        ("categories", _import_categories),
        ("groups", _import_groups),
        ("marketGroups", _import_market_groups),
        ("metaGroups", _import_meta_groups),
        ("types", _import_types),
        ("regions", _import_regions),
        ("systems", _import_systems),
        ("stations", _import_stations),
    ]
    with db.transaction(conn):
        for i, (label, fn) in enumerate(steps):
            if progress:
                progress(f"Importing {label}", int(i * 100 / len(steps)))
            fn(conn, zf)
        db.set_meta(conn, "sde_build", str(build))
    conn.execute("ANALYZE")
    if progress:
        progress(f"SDE {build} imported", 100)


def _import_categories(conn, zf):
    conn.execute("DELETE FROM sde_categories")
    db.upsert_many(
        conn, "sde_categories", ["category_id", "name", "published"],
        (
            (r["_key"], _en(r), int(bool(r.get("published"))))
            for r in _records(zf, "categories.jsonl")
        ),
    )


def _import_groups(conn, zf):
    conn.execute("DELETE FROM sde_groups")
    db.upsert_many(
        conn, "sde_groups", ["group_id", "category_id", "name", "published"],
        (
            (r["_key"], r.get("categoryID"), _en(r), int(bool(r.get("published"))))
            for r in _records(zf, "groups.jsonl")
        ),
    )


def _import_market_groups(conn, zf):
    conn.execute("DELETE FROM sde_market_groups")
    db.upsert_many(
        conn, "sde_market_groups", ["market_group_id", "parent_id", "name"],
        (
            (r["_key"], r.get("parentGroupID"), _en(r))
            for r in _records(zf, "marketGroups.jsonl")
        ),
    )


def _import_meta_groups(conn, zf):
    conn.execute("DELETE FROM sde_meta_groups")
    db.upsert_many(
        conn, "sde_meta_groups", ["meta_group_id", "name"],
        ((r["_key"], _en(r)) for r in _records(zf, "metaGroups.jsonl")),
    )


def _import_types(conn, zf):
    conn.execute("DELETE FROM sde_types")
    cols = [
        "type_id", "name", "group_id", "market_group_id", "meta_group_id",
        "volume", "capacity", "portion_size", "base_price", "published",
    ]
    db.upsert_many(
        conn, "sde_types", cols,
        (
            (
                r["_key"], _en(r), r.get("groupID"), r.get("marketGroupID"),
                r.get("metaGroupID"), r.get("volume"), r.get("capacity"),
                r.get("portionSize") or 1, r.get("basePrice") or 0.0,
                int(bool(r.get("published"))),
            )
            for r in _records(zf, "types.jsonl")
        ),
    )


def _import_regions(conn, zf):
    conn.execute("DELETE FROM sde_regions")
    db.upsert_many(
        conn, "sde_regions", ["region_id", "name"],
        ((r["_key"], _en(r)) for r in _records(zf, "mapRegions.jsonl")),
    )


def _import_systems(conn, zf):
    conn.execute("DELETE FROM sde_systems")
    db.upsert_many(
        conn, "sde_systems", ["system_id", "name", "constellation_id", "region_id", "security"],
        (
            (
                r["_key"], _en(r), r.get("constellationID"), r.get("regionID"),
                r.get("securityStatus"),
            )
            for r in _records(zf, "mapSolarSystems.jsonl")
        ),
    )


def _import_stations(conn, zf):
    """NPC station names are not in the SDE; CCP documents how to build them.

    station = "<orbitName> - <corpName> <operationName>", where orbitName is
    "<system> <roman(celestialIndex)>" for a planet and that plus
    " - Moon <orbitIndex>" for a moon.
    """
    systems = {
        r["system_id"]: (r["name"], r["region_id"])
        for r in conn.execute("SELECT system_id, name, region_id FROM sde_systems")
    }
    corps = {r["_key"]: _en(r) for r in _records(zf, "npcCorporations.jsonl")}
    ops = {r["_key"]: _en(r, "operationName") for r in _records(zf, "stationOperations.jsonl")}

    def rows():
        for r in _records(zf, "npcStations.jsonl"):
            sys_id = r.get("solarSystemID")
            sys_name, region_id = systems.get(sys_id, ("Unknown", None))
            orbit = f"{sys_name} {roman(r.get('celestialIndex') or 0)}".strip()
            if r.get("orbitIndex"):
                orbit = f"{orbit} - Moon {r['orbitIndex']}"
            corp = corps.get(r.get("ownerID"), "")
            if r.get("useOperationName"):
                op = ops.get(r.get("operationID"), "")
                suffix = f"{corp} {op}".strip()
            else:
                suffix = corp
            name = f"{orbit} - {suffix}" if suffix else orbit
            yield (r["_key"], name, sys_id, region_id)

    conn.execute("DELETE FROM sde_stations")
    db.upsert_many(conn, "sde_stations", ["station_id", "name", "system_id", "region_id"], rows())



# --------------------------------------------------------- update checking
# Keys in the meta table. The last check is remembered so a launch does not
# ask CCP every time, and a declined build is remembered so "not now" means
# not now rather than "ask me again in ninety seconds".
META_CHECKED_AT = "sde_checked_at"
META_SKIPPED_BUILD = "sde_skipped_build"

# The check itself costs one 80-byte GET, so this interval is not about load.
# It is about a user who opens the app six times in an afternoon not being
# asked six times.
CHECK_INTERVAL = timedelta(hours=6)


@dataclass(frozen=True)
class SdeStatus:
    """What a startup check found."""

    installed: int | None
    latest: int

    @property
    def missing(self) -> bool:
        """Never imported. The app cannot show a single asset in this state:
        ASSET_ROWS inner joins sde_types, so an empty SDE renders an empty
        table however much has been synced."""
        return self.installed is None

    @property
    def stale(self) -> bool:
        return self.installed is not None and self.installed < self.latest

    @property
    def needed(self) -> bool:
        return self.missing or self.stale


def due_for_check(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    """Whether enough time has passed to ask CCP again.

    Always true when nothing is imported: that state is broken rather than
    merely out of date, and it should be raised on the very next launch
    however recently it was checked.
    """
    if installed_build(conn) is None:
        return True
    stamp = db.get_meta(conn, META_CHECKED_AT)
    if not stamp:
        return True
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return (now or datetime.now(timezone.utc)) - last >= CHECK_INTERVAL


def check(conn: sqlite3.Connection, settings: Settings | None = None) -> SdeStatus:
    """Ask CCP for the current build and compare. No download."""
    status = SdeStatus(installed=installed_build(conn), latest=latest_build(settings))
    db.set_meta(conn, META_CHECKED_AT, datetime.now(timezone.utc).isoformat())
    return status


def skip_build(conn: sqlite3.Connection, build: int) -> None:
    """Remember that this build was declined, so it is not offered again."""
    db.set_meta(conn, META_SKIPPED_BUILD, str(build))


def was_skipped(conn: sqlite3.Connection, build: int) -> bool:
    return db.get_meta(conn, META_SKIPPED_BUILD) == str(build)


def ensure_current(
    conn: sqlite3.Connection, settings: Settings | None = None, progress: Progress | None = None
) -> tuple[bool, int]:
    """Download and import the SDE if the local copy is stale.

    Returns (updated, build_number).
    """
    if progress:
        progress("Checking for SDE updates", 0)
    build = latest_build(settings)
    if installed_build(conn) == build:
        if progress:
            progress(f"SDE {build} already current", 100)
        return False, build
    path = download(build, settings, progress)
    import_zip(conn, path, build, progress)
    return True, build
