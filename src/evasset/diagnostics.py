"""A support summary a user can paste into an issue.

Written after a first-time user reported an empty Assets tab holding 32,297
stacks. Everything needed to solve it was already on their screen -- the
status bar said "SDE build not imported" -- but the interesting number was the
one nowhere on screen: sde_types had zero rows, and the assets query inner
joins it, so every asset was filtered out before the table ever saw it.

So this is deliberately not a log of what happened. It is a snapshot of what
*is*, weighted towards the questions that turn "the app is broken" into a
diagnosis:

  which build is this            a stale build explains bugs already fixed
  is the game data imported      an empty sde_types empties the whole tab
  how many rows in each table    "synced but empty" and "never synced" look
                                 identical from a screenshot, and are not
  what did the last sync say     characters carry their own last_error
  which paths are in use         a stray EVEBOOTY_DATA_DIR sends the app at a
                                 database nobody is looking at

Qt-free so the headless CLI can print it, and so it can be gathered when the
GUI is the thing that is broken.

Nothing here may include a credential. The client id is public (see config)
and is shown because a wrong one is a real cause of failure; the secret is
reported only as set or unset, and refresh tokens are never read at all.
"""

from __future__ import annotations

import os
import platform
import sqlite3
import sys
from datetime import datetime, timezone

from . import __version__
from .config import CACHE_DIR, DATA_DIR, DB_PATH, SETTINGS_PATH, Settings

# Ordered for reading rather than alphabetically: the tables that answer "did
# a sync happen and did it land" first, then the static data, then the rest.
TABLES = (
    "characters",
    "corporations",
    "assets",
    "prices",
    "wallets",
    "wallet_journal",
    "wallet_transactions",
    "market_orders",
    "contracts",
    "industry_jobs",
    "blueprints",
    "structures",
    "networth_snapshots",
    "stockpiles",
    "stockpile_items",
    "sde_types",
    "sde_groups",
    "sde_categories",
    "sde_stations",
    "sde_systems",
    "sde_regions",
)

# Empty here means the Assets tab is empty no matter how many assets synced,
# because ASSET_ROWS inner joins sde_types.
CRITICAL_IF_EMPTY = ("sde_types",)


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None


def _meta(conn: sqlite3.Connection, key: str) -> str:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return "?"
    return (row[0] if not hasattr(row, "keys") else row["value"]) if row else "not set"


def problems(conn: sqlite3.Connection) -> list[str]:
    """Conclusions, not measurements.

    The point of the report is that a user should not have to interpret it.
    If the numbers already say what is wrong, say it in words at the top.
    """
    found = []
    assets = _count(conn, "assets") or 0
    types = _count(conn, "sde_types") or 0
    chars = _count(conn, "characters") or 0

    if types == 0:
        found.append(
            "Game data (the SDE) has not been imported, so sde_types is empty. "
            "The Assets tab inner joins it, which is why the table is blank "
            "even with assets synced. Fix: Update -> Game data."
        )
    if chars == 0:
        found.append("No characters have been added, so nothing can sync.")
    elif assets == 0:
        found.append(
            "Characters exist but no assets are stored. Either no sync has "
            "run, or every asset pull failed -- check last_error below."
        )
    if assets and types and (_count(conn, "prices") or 0) == 0:
        found.append(
            "No prices stored, so every value column reads 0. "
            "Fix: Update -> Prices."
        )
    return found


def report(conn: sqlite3.Connection, settings: Settings | None = None) -> str:
    """The whole summary, as text meant to be pasted into an issue."""
    settings = settings or Settings.load()
    out: list[str] = []
    add = out.append

    add("EVE Booty diagnostics")
    add(f"generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    add("")

    issues = problems(conn)
    if issues:
        add("PROBLEMS FOUND")
        for i, line in enumerate(issues, 1):
            add(f"  {i}. {line}")
        add("")

    add("BUILD")
    add(f"  version        {__version__}")
    add(f"  frozen         {bool(getattr(sys, 'frozen', False)) or '__compiled__' in globals()}")
    add(f"  python         {platform.python_version()}")
    add(f"  platform       {platform.platform()}")
    try:
        from PySide6 import __version__ as qt_version

        add(f"  PySide6        {qt_version}")
    except Exception:  # noqa: BLE001 - reporting must not depend on Qt
        add("  PySide6        not importable")
    add("")

    add("PATHS")
    add(f"  data dir       {DATA_DIR}")
    add(f"  cache dir      {CACHE_DIR}")
    add(f"  database       {DB_PATH}")
    try:
        add(f"  database size  {DB_PATH.stat().st_size:,} bytes")
    except OSError:
        add("  database size  missing")
    add(f"  settings       {SETTINGS_PATH}")
    for var in ("EVEBOOTY_DATA_DIR", "EVEBOOTY_CACHE_DIR", "EVASSET_DATA_DIR", "EVASSET_CACHE_DIR"):
        if os.environ.get(var):
            add(f"  {var}={os.environ[var]}   (override in effect)")
    add("")

    add("DATA")
    add(f"  schema version {_meta(conn, 'schema_version')}")
    add(f"  SDE build      {_meta(conn, 'sde_build')}")
    width = max(len(t) for t in TABLES)
    for table in TABLES:
        n = _count(conn, table)
        flag = ""
        if n == 0 and table in CRITICAL_IF_EMPTY:
            flag = "   <-- EMPTY, this breaks the Assets tab"
        elif n is None:
            flag = "   <-- table missing"
        add(f"  {table:<{width}}  {n if n is not None else '?':>9}{flag}")
    add("")

    add("SETTINGS")
    add(f"  client id      {settings.client_id or '(none)'}")
    add(f"  client secret  {'set' if settings.client_secret else 'not set (PKCE, expected)'}")
    add(f"  callback       {settings.redirect_uri}")
    add(f"  contact email  {'set' if settings.contact_email else 'not set'}")
    add(f"  snapshot sync  {settings.snapshot_on_sync}")
    add("")

    add("CHARACTERS")
    try:
        rows = list(
            conn.execute(
                "SELECT name, enabled, include_corp, last_sync_at, last_error,"
                " length(COALESCE(scopes,'')) AS scope_len"
                " FROM characters ORDER BY name"
            )
        )
    except sqlite3.Error as exc:
        rows = []
        add(f"  could not read characters: {exc}")
    if not rows:
        add("  none")
    for r in rows:
        add(
            f"  {r['name']}  enabled={r['enabled']}  corp={r['include_corp']}"
            f"  last_sync={r['last_sync_at'] or 'never'}  scopes={r['scope_len']}b"
        )
        if r["last_error"]:
            for line in str(r["last_error"]).splitlines():
                add(f"      ! {line}")
    return "\n".join(out)
