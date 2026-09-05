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
import re
import sqlite3
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import db
from .config import CACHE_DIR, SDE_BUILD_URL, SDE_LATEST_URL, Settings, user_agent
from .logsetup import LOGGER

Progress = Callable[[str, int], None]  # (message, percent 0-100)

# Which tables import_zip fills, independent of CCP's build number. Bump it
# whenever a step is added or an existing table gains a column the importer
# now reads, so an install that already has the current build re-imports
# from its cached zip instead of waiting for CCP to ship a new build. Version
# 1 is the implicit pre-dogma state; 2 added the dogma tables and
# sde_types.is_dynamic_type.
SDE_TABLES_VERSION = 2
_TABLES_VERSION_KEY = "sde_tables_version"

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


def latest_build(settings: Settings | None = None, timeout: float = 30.0) -> int:
    LOGGER.debug("SDE: asking CCP for the current build (timeout %.1fs)", timeout)
    r = httpx.get(
        SDE_LATEST_URL, headers={"User-Agent": user_agent(settings)}, timeout=timeout
    )
    r.raise_for_status()
    for line in r.text.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("_key") == "sde":
            build = int(rec["buildNumber"])
            LOGGER.info("SDE: CCP reports build %s", build)
            return build
    raise RuntimeError("no 'sde' record in latest.jsonl")


def installed_build(conn: sqlite3.Connection) -> int | None:
    """The imported SDE build, or None when the tables need (re)importing.

    Reports None not only when nothing was ever imported but also when the
    import was made by an older code version that filled fewer tables
    (tables_stale). ensure_current compares this with CCP's latest build and
    skips the import on a match, so a stale table set on a current build
    would otherwise never be filled until CCP happened to publish a new
    build. The raw build number for display lives in queries.sde_build, and
    recorded_build below answers the same question for callers that only want
    to know whether CCP has anything newer.
    """
    if tables_stale(conn):
        return None
    return recorded_build(conn)


def recorded_build(conn: sqlite3.Connection) -> int | None:
    """The build the last import wrote down, whatever state the tables are in.

    installed_build deliberately reports None once this build's importer
    reads tables an older import never filled, which is right for
    ensure_current but wrong for the startup update check: a stale table set
    is refreshed locally from the cached zip, so reporting it as "no game
    data at all" would offer a 95 MB download to somebody whose SDE is
    already current.
    """
    val = db.get_meta(conn, "sde_build")
    return int(val) if val else None


def tables_stale(conn: sqlite3.Connection) -> bool:
    """True when an SDE is installed but was imported by an older importer.

    A fresh database with no SDE at all is not "stale": there is nothing to
    re-import from, and the first-run flow already handles downloading one.
    This is the startup trigger for the automatic re-import from the cached
    zip -- see ensure_current, which reaches the same conclusion through
    installed_build.
    """
    if not db.get_meta(conn, "sde_build"):
        return False
    return db.get_meta(conn, _TABLES_VERSION_KEY) != str(SDE_TABLES_VERSION)


def download(build: int, settings: Settings | None = None, progress: Progress | None = None) -> Path:
    dest = CACHE_DIR / f"sde-{build}.zip"
    LOGGER.info("SDE: download build %s -> %s", build, dest)
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
        ("dogma attributes", _import_dogma_attributes),
        ("dogma units", _import_dogma_units),
        ("mutator ranges", _import_mutator_ranges),
        ("type dogma", _import_type_dogma),
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
        db.set_meta(conn, _TABLES_VERSION_KEY, str(SDE_TABLES_VERSION))
        LOGGER.info("SDE: build %s imported", build)
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
        "is_dynamic_type",
    ]
    db.upsert_many(
        conn, "sde_types", cols,
        (
            (
                r["_key"], _en(r), r.get("groupID"), r.get("marketGroupID"),
                r.get("metaGroupID"), r.get("volume"), r.get("capacity"),
                r.get("portionSize") or 1, r.get("basePrice") or 0.0,
                int(bool(r.get("published"))),
                # Added to types.jsonl in build 3464040 (2026-08-12); a zip
                # older than that simply lacks the key and every type reads 0.
                int(bool(r.get("isDynamicType"))),
            )
            for r in _records(zf, "types.jsonl")
        ),
    )


def _import_dogma_attributes(conn, zf):
    conn.execute("DELETE FROM sde_dogma_attributes")
    cols = [
        "attribute_id", "name", "display_name", "unit_id", "high_is_good",
        "default_value", "published",
    ]

    def rows():
        for r in _records(zf, "dogmaAttributes.jsonl"):
            # displayName is a localisation dict on the 1,248 attributes that
            # have one and absent on the rest; name is always a plain string.
            display = _en(r, "displayName") or None
            high = r.get("highIsGood")
            yield (
                r["_key"], r.get("name") or "", display, r.get("unitID"),
                None if high is None else int(bool(high)),
                r.get("defaultValue"), int(bool(r.get("published"))),
            )

    db.upsert_many(conn, "sde_dogma_attributes", cols, rows())


def _import_dogma_units(conn, zf):
    conn.execute("DELETE FROM sde_dogma_units")
    db.upsert_many(
        conn, "sde_dogma_units", ["unit_id", "name", "display_name"],
        (
            (r["_key"], r.get("name") or "", _en(r, "displayName") or None)
            for r in _records(zf, "dogmaUnits.jsonl")
        ),
    )


def _import_mutator_ranges(conn, zf):
    """Flatten dynamicItemAttributes: one row per (mutaplasmid, attribute).

    Every record in build 3487903 carries exactly one inputOutputMapping
    entry, so resultingType is taken from the first; a record with none
    (should CCP ever add a mutaplasmid with no output) stores NULL rather
    than being skipped, because its ranges are still what the inspector
    needs once ESI names it as an item's mutator.
    """
    conn.execute("DELETE FROM sde_mutator_ranges")
    cols = [
        "mutator_type_id", "attribute_id", "min_mult", "max_mult", "high_is_good",
        "resulting_type_id",
    ]

    def rows():
        for r in _records(zf, "dynamicItemAttributes.jsonl"):
            mapping = r.get("inputOutputMapping") or []
            resulting = mapping[0].get("resultingType") if mapping else None
            for attr in r.get("attributeIDs") or []:
                high = attr.get("highIsGood")
                yield (
                    r["_key"], attr["_key"], attr["min"], attr["max"],
                    None if high is None else int(bool(high)), resulting,
                )

    db.upsert_many(conn, "sde_mutator_ranges", cols, rows())


def _mutable_type_ids(zf) -> set[int]:
    """Every type a mutaplasmid can be applied to, plus every type it makes."""
    ids: set[int] = set()
    for r in _records(zf, "dynamicItemAttributes.jsonl"):
        for mapping in r.get("inputOutputMapping") or []:
            ids.update(mapping.get("applicableTypes") or [])
            if mapping.get("resultingType") is not None:
                ids.add(mapping["resultingType"])
    return ids


_TYPE_DOGMA_KEY = re.compile(rb'^\{"_key":\s*(\d+)')


def _import_type_dogma(conn, zf):
    """Base attribute values, but only for types a mutaplasmid can touch.

    typeDogma.jsonl covers all 26.8k types and is the largest file in the
    zip; the rolls display only ever needs the source type's base values
    (the abyssal type itself carries almost no dogma, verified against
    build 3487903), so the import is restricted to applicableTypes plus
    resultingType -- a few hundred types. The restriction set is re-read
    from dynamicItemAttributes.jsonl (166 KB) rather than passed from the
    previous step, so each step stays independently re-runnable.
    """
    wanted = _mutable_type_ids(zf)
    conn.execute("DELETE FROM sde_type_dogma")

    def rows():
        # Parsing all 26,828 lines costs 270 ms to keep 1,110 of them; the
        # key is the first field of every line, so a prefix match skips the
        # parser for the rest (86 ms, build 3487903). A line in any other
        # shape is parsed as before, so a format change costs time, not rows.
        with zf.open("typeDogma.jsonl") as fh:
            for line in fh:
                match = _TYPE_DOGMA_KEY.match(line)
                if match is not None and int(match.group(1)) not in wanted:
                    continue
                if not line.strip():
                    continue
                r = json.loads(line)
                if r["_key"] not in wanted:
                    continue
                for attr in r.get("dogmaAttributes") or []:
                    yield (r["_key"], attr["attributeID"], attr["value"])

    db.upsert_many(conn, "sde_type_dogma", ["type_id", "attribute_id", "value"], rows())


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

# How long a startup staleness check may take before giving up.
#
# Nothing waits on it -- it runs on the thread pool -- but the 30 second
# default parks that thread and a task bar entry for half a minute whenever
# CCP is slow, to learn something only worth knowing promptly.
#
# Five, not one. A cold connection is roughly four round trips before the
# server does any work: DNS, TCP, TLS, then the request. Measured from a
# European connection that is 163ms end to end, but at the 250ms RTT of a
# Tokyo-to-Iceland link it is a full second of floor, so a one second budget
# would fail exactly the users furthest from CCP and never fail anyone near
# it. Five leaves real headroom over that floor while staying nowhere near
# thirty.
#
# Being generous is cheap here because the case that actually matters never
# makes the call: whether the SDE is missing is a local fact, and staleness
# can always wait for the next launch.
STARTUP_TIMEOUT = 5.0


@dataclass(frozen=True)
class SdeStatus:
    """What a startup check found.

    latest is None when CCP was not asked, or did not answer in time. That is
    not a failure state: a missing SDE is established locally and needs no
    network, and an unanswered staleness check simply means asking again
    later.
    """

    installed: int | None
    latest: int | None

    @property
    def missing(self) -> bool:
        """Never imported. The app cannot show a single asset in this state:
        ASSET_ROWS inner joins sde_types, so an empty SDE renders an empty
        table however much has been synced."""
        return self.installed is None

    @property
    def stale(self) -> bool:
        if self.installed is None or self.latest is None:
            return False
        return self.installed < self.latest

    @property
    def needed(self) -> bool:
        return self.missing or self.stale


def due_for_check(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    """Whether enough time has passed to ask CCP again.

    Always true when nothing is imported: that state is broken rather than
    merely out of date, and it should be raised on the very next launch
    however recently it was checked. recorded_build rather than
    installed_build, because a stale table set is not that state: it is
    re-imported locally at startup and needs no question asked of CCP.
    """
    if recorded_build(conn) is None:
        return True
    stamp = db.get_meta(conn, META_CHECKED_AT)
    if not stamp:
        return True
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return (now or datetime.now(timezone.utc)) - last >= CHECK_INTERVAL


def check(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    timeout: float = 30.0,
) -> SdeStatus:
    """Compare the installed build against CCP's, without downloading one.

    The network is skipped entirely when nothing is installed. That state is
    already conclusive -- the app cannot display an asset either way -- so
    asking CCP for a number nobody will read only adds a way for the answer to
    arrive late or not at all.

    A network failure is not raised. It leaves latest as None, which reads as
    "not stale as far as we know", and the check runs again next time.

    "Installed" here is the recorded build, not installed_build: an install
    whose dogma tables predate this importer still has every type name and
    volume, so it is out of date at worst and never missing.
    """
    installed = recorded_build(conn)
    if installed is None:
        LOGGER.info("SDE: nothing imported; no version check needed")
        return SdeStatus(installed=None, latest=None)

    try:
        latest = latest_build(settings, timeout=timeout)
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        LOGGER.warning("SDE: version check failed (%s); will retry later", exc)
        return SdeStatus(installed=installed, latest=None)

    db.set_meta(conn, META_CHECKED_AT, datetime.now(timezone.utc).isoformat())
    LOGGER.info("SDE: installed %s, latest %s -> %s",
                installed, latest, "update available" if installed < latest else "current")
    return SdeStatus(installed=installed, latest=latest)


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
    LOGGER.info("SDE: ensure_current -- installed %s, latest %s",
                installed_build(conn), build)
    if installed_build(conn) == build:
        if progress:
            progress(f"SDE {build} already current", 100)
        return False, build
    path = download(build, settings, progress)
    import_zip(conn, path, build, progress)
    return True, build
