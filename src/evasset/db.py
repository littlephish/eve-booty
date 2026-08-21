"""SQLite storage: schema, migrations, connection helper."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---------------------------------------------------------------- static data
CREATE TABLE IF NOT EXISTS sde_categories (
    category_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    published   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sde_groups (
    group_id    INTEGER PRIMARY KEY,
    category_id INTEGER,
    name        TEXT NOT NULL,
    published   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sde_market_groups (
    market_group_id INTEGER PRIMARY KEY,
    parent_id       INTEGER,
    name            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sde_meta_groups (
    meta_group_id INTEGER PRIMARY KEY,
    name          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sde_types (
    type_id         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    group_id        INTEGER,
    market_group_id INTEGER,
    meta_group_id   INTEGER,
    volume          REAL,
    capacity        REAL,
    portion_size    INTEGER,
    base_price      REAL,
    published       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_types_name  ON sde_types(name);
CREATE INDEX IF NOT EXISTS idx_types_group ON sde_types(group_id);

CREATE TABLE IF NOT EXISTS sde_regions (
    region_id INTEGER PRIMARY KEY,
    name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sde_systems (
    system_id        INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    constellation_id INTEGER,
    region_id        INTEGER,
    security         REAL
);
CREATE INDEX IF NOT EXISTS idx_systems_region ON sde_systems(region_id);

CREATE TABLE IF NOT EXISTS sde_stations (
    station_id INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    system_id  INTEGER,
    region_id  INTEGER
);

-- Player-owned structures; names come from ESI and need docking access.
CREATE TABLE IF NOT EXISTS structures (
    structure_id INTEGER PRIMARY KEY,
    name         TEXT,
    system_id    INTEGER,
    region_id    INTEGER,
    type_id      INTEGER,
    owner_id     INTEGER,
    resolved_at  TEXT,
    accessible   INTEGER NOT NULL DEFAULT 1
);

-- ------------------------------------------------------------------ accounts
CREATE TABLE IF NOT EXISTS characters (
    character_id     INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    corporation_id   INTEGER,
    corporation_name TEXT,
    alliance_id      INTEGER,
    scopes           TEXT,
    added_at         TEXT,
    last_sync_at     TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    include_corp     INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT
);

CREATE TABLE IF NOT EXISTS corporations (
    corporation_id INTEGER PRIMARY KEY,
    name           TEXT,
    ticker         TEXT,
    -- character whose token is used to read this corp's data
    via_character_id INTEGER,
    last_sync_at   TEXT
);

CREATE TABLE IF NOT EXISTS corp_divisions (
    corporation_id INTEGER NOT NULL,
    kind           TEXT NOT NULL,   -- 'hangar' | 'wallet'
    division       INTEGER NOT NULL,
    name           TEXT,
    PRIMARY KEY (corporation_id, kind, division)
);

-- --------------------------------------------------------------------- data
-- owner_type is 'character' or 'corporation'; owner_id the matching id.
CREATE TABLE IF NOT EXISTS assets (
    owner_type    TEXT NOT NULL,
    owner_id      INTEGER NOT NULL,
    item_id       INTEGER NOT NULL,
    type_id       INTEGER NOT NULL,
    quantity      INTEGER NOT NULL,
    location_id   INTEGER NOT NULL,
    location_flag TEXT,
    location_type TEXT,
    is_singleton  INTEGER NOT NULL DEFAULT 0,
    is_blueprint_copy INTEGER NOT NULL DEFAULT 0,
    custom_name   TEXT,
    -- resolved by flattening the container tree
    root_location_id INTEGER,
    system_id     INTEGER,
    region_id     INTEGER,
    PRIMARY KEY (owner_type, owner_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_assets_type  ON assets(type_id);
CREATE INDEX IF NOT EXISTS idx_assets_loc   ON assets(root_location_id);
CREATE INDEX IF NOT EXISTS idx_assets_owner ON assets(owner_type, owner_id);
-- location_id (the *direct* parent, not the flattened root) has no index of
-- its own until now. Nothing needed it before; "what is fit to/in this ship"
-- is exactly a WHERE location_id = ? lookup, and without this it is a full
-- table scan of everything you own every time someone opens the fit dialog.
CREATE INDEX IF NOT EXISTS idx_assets_direct_loc ON assets(location_id);

CREATE TABLE IF NOT EXISTS wallets (
    owner_type TEXT NOT NULL,
    owner_id   INTEGER NOT NULL,
    division   INTEGER NOT NULL DEFAULT 1,
    balance    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (owner_type, owner_id, division)
);

CREATE TABLE IF NOT EXISTS market_orders (
    owner_type    TEXT NOT NULL,
    owner_id      INTEGER NOT NULL,
    order_id      INTEGER NOT NULL,
    type_id       INTEGER NOT NULL,
    location_id   INTEGER,
    region_id     INTEGER,
    is_buy_order  INTEGER NOT NULL DEFAULT 0,
    price         REAL,
    volume_remain INTEGER,
    volume_total  INTEGER,
    escrow        REAL,
    issued        TEXT,
    PRIMARY KEY (owner_type, owner_id, order_id)
);

CREATE TABLE IF NOT EXISTS contracts (
    owner_type   TEXT NOT NULL,
    owner_id     INTEGER NOT NULL,
    contract_id  INTEGER NOT NULL,
    type         TEXT,
    status       TEXT,
    issuer_id    INTEGER,
    issuer_corporation_id INTEGER,
    assignee_id  INTEGER,
    for_corporation INTEGER,
    availability TEXT,
    price        REAL,
    reward       REAL,
    collateral   REAL,
    volume       REAL,
    start_location_id INTEGER,
    end_location_id   INTEGER,
    date_issued  TEXT,
    date_expired TEXT,
    title        TEXT,
    PRIMARY KEY (owner_type, owner_id, contract_id)
);

CREATE TABLE IF NOT EXISTS contract_items (
    contract_id  INTEGER NOT NULL,
    record_id    INTEGER NOT NULL,
    type_id      INTEGER NOT NULL,
    quantity     INTEGER NOT NULL,
    is_included  INTEGER NOT NULL DEFAULT 1,
    is_singleton INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (contract_id, record_id)
);

CREATE TABLE IF NOT EXISTS industry_jobs (
    owner_type      TEXT NOT NULL,
    owner_id        INTEGER NOT NULL,
    job_id          INTEGER NOT NULL,
    installer_id    INTEGER,
    activity_id     INTEGER,
    blueprint_type_id INTEGER,
    blueprint_location_id INTEGER,
    output_location_id INTEGER,
    facility_id     INTEGER,
    product_type_id INTEGER,
    runs            INTEGER,
    licensed_runs   INTEGER,
    cost            REAL,
    status          TEXT,
    start_date      TEXT,
    end_date        TEXT,
    PRIMARY KEY (owner_type, owner_id, job_id)
);

CREATE TABLE IF NOT EXISTS blueprints (
    owner_type          TEXT NOT NULL,
    owner_id            INTEGER NOT NULL,
    item_id             INTEGER NOT NULL,
    type_id             INTEGER NOT NULL,
    location_id         INTEGER,
    location_flag       TEXT,
    quantity            INTEGER,
    material_efficiency INTEGER,
    time_efficiency     INTEGER,
    runs                INTEGER,
    PRIMARY KEY (owner_type, owner_id, item_id)
);

-- ------------------------------------------------------------ wallet history
-- Journal and transactions are append-only. ESI only serves roughly the last
-- 30 days (and at most 2500 rows), so the local copy becomes the long history
-- as long as you sync often enough. Never DELETE these on sync.
CREATE TABLE IF NOT EXISTS wallet_journal (
    owner_type      TEXT NOT NULL,
    owner_id        INTEGER NOT NULL,
    division        INTEGER NOT NULL DEFAULT 1,
    entry_id        INTEGER NOT NULL,
    date            TEXT NOT NULL,
    ref_type        TEXT,
    amount          REAL,
    balance         REAL,
    description     TEXT,
    reason          TEXT,
    first_party_id  INTEGER,
    second_party_id INTEGER,
    context_id      INTEGER,
    context_id_type TEXT,
    tax             REAL,
    tax_receiver_id INTEGER,
    PRIMARY KEY (owner_type, owner_id, division, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_journal_date ON wallet_journal(date);
CREATE INDEX IF NOT EXISTS idx_journal_ref  ON wallet_journal(ref_type);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    owner_type     TEXT NOT NULL,
    owner_id       INTEGER NOT NULL,
    division       INTEGER NOT NULL DEFAULT 1,
    transaction_id INTEGER NOT NULL,
    date           TEXT NOT NULL,
    type_id        INTEGER NOT NULL,
    quantity       INTEGER NOT NULL,
    unit_price     REAL NOT NULL,
    is_buy         INTEGER NOT NULL DEFAULT 0,
    is_personal    INTEGER,
    client_id      INTEGER,
    location_id    INTEGER,
    journal_ref_id INTEGER,
    PRIMARY KEY (owner_type, owner_id, division, transaction_id)
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON wallet_transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_type ON wallet_transactions(type_id);

-- Resolved character/corp/alliance names for journal counterparties.
CREATE TABLE IF NOT EXISTS names (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT,
    updated_at TEXT
);

-- -------------------------------------------------------------------- prices
-- Two bases, always. buy_price is the highest Jita bid (what you get dumping
-- everything today); sell_price is the lowest Jita ask (what you get with
-- patience). For a hull priced off public contracts there is no bid/ask, just
-- one number, so both columns carry the same figure and `source` says so.
CREATE TABLE IF NOT EXISTS prices (
    type_id     INTEGER PRIMARY KEY,
    buy_price   REAL NOT NULL DEFAULT 0,
    sell_price  REAL NOT NULL DEFAULT 0,
    source      TEXT NOT NULL,      -- 'jita' | 'contract_avg' | 'base_price'
    samples     INTEGER,
    updated_at  TEXT NOT NULL
);

-- --------------------------------------------------------------- net worth
-- Item-valued buckets (assets, contracts, in-production) are recorded twice,
-- once at Jita buy and once at Jita sell. Wallet, sell orders and escrow are
-- already ISK, so they are basis-independent and stored once.
CREATE TABLE IF NOT EXISTS networth_snapshots (
    snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at          TEXT NOT NULL,
    owner_type        TEXT NOT NULL,
    owner_id          INTEGER NOT NULL,
    assets_buy_isk    REAL NOT NULL DEFAULT 0,
    assets_sell_isk   REAL NOT NULL DEFAULT 0,
    wallet_isk        REAL NOT NULL DEFAULT 0,
    orders_isk        REAL NOT NULL DEFAULT 0,
    escrow_isk        REAL NOT NULL DEFAULT 0,
    contracts_buy_isk REAL NOT NULL DEFAULT 0,
    contracts_sell_isk REAL NOT NULL DEFAULT 0,
    jobs_buy_isk      REAL NOT NULL DEFAULT 0,
    jobs_sell_isk     REAL NOT NULL DEFAULT 0,
    total_buy_isk     REAL NOT NULL DEFAULT 0,
    total_sell_isk    REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snap_owner ON networth_snapshots(owner_type, owner_id, taken_at);

-- ----------------------------------------------------------- http etag cache
CREATE TABLE IF NOT EXISTS http_cache (
    url        TEXT PRIMARY KEY,
    etag       TEXT,
    expires_at TEXT,
    body       BLOB
);
"""

_local = threading.local()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """One connection per thread. Qt workers each get their own."""
    key = str(path or DB_PATH)
    cached = getattr(_local, "conns", None)
    if cached is None:
        cached = _local.conns = {}
    conn = cached.get(key)
    if conn is None:
        conn = sqlite3.connect(key, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        cached[key] = conn
    return conn


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _rebuild_table(
    conn: sqlite3.Connection,
    table: str,
    create_sql: str,
    copy_sql: str,
) -> None:
    """Rename, recreate in the target shape, copy data across, drop the old one.

    ALTER TABLE ADD COLUMN looks tempting for a migration like this, but it
    cannot remove or re-constrain a column -- an old NOT NULL column with no
    DEFAULT stays exactly as demanding as it always was, and the first insert
    that does not mention it (which is every insert this app makes once the
    new columns are in charge) hits an IntegrityError. Rebuilding the table in
    the exact target shape and copying the data across sidesteps that
    entirely, and works on any SQLite version -- no DROP COLUMN support
    required.
    """
    old = f"{table}__migrating"
    conn.execute(f"ALTER TABLE {table} RENAME TO {old}")
    conn.execute(create_sql)
    conn.execute(copy_sql.format(old=old))
    conn.execute(f"DROP TABLE {old}")


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Bring an older database up to the current schema in place.

    Returns a list of what it did, for the log. CREATE TABLE IF NOT EXISTS
    handles brand new tables on its own; this only deals with tables that
    changed shape and already had rows worth keeping. The target shape here
    must be kept in sync with the CREATE TABLE statements in SCHEMA above --
    CREATE TABLE IF NOT EXISTS never touches an existing table, so this
    function is the only thing that actually gets an upgraded database to
    match what SCHEMA describes.
    """
    done: list[str] = []

    # v1 -> v2: one price column became a buy/sell pair.
    #
    # Trigger on "price" being present at all, not on "buy_price" being
    # absent. A database that already limped through the first version of
    # this migration (the one that used ALTER TABLE ADD COLUMN, before it was
    # rewritten to rebuild the table) has buy_price and sell_price already
    # added *alongside* the still-dangling price column -- checking for
    # "buy_price" not in cols would see those leftover columns and conclude,
    # wrongly, that nothing needs fixing, leaving the NOT NULL price column in
    # place forever and reproducing the exact crash this migration exists to
    # prevent. price simply should not exist once this table is current, full
    # stop, regardless of what else has already been bolted on next to it.
    if _table_exists(conn, "prices"):
        cols = columns(conn, "prices")
        if "price" in cols:
            _rebuild_table(
                conn,
                "prices",
                """CREATE TABLE prices (
                       type_id     INTEGER PRIMARY KEY,
                       buy_price   REAL NOT NULL DEFAULT 0,
                       sell_price  REAL NOT NULL DEFAULT 0,
                       source      TEXT NOT NULL,
                       samples     INTEGER,
                       updated_at  TEXT NOT NULL
                   )""",
                # The old single column was the Jita sell price. Seed sell
                # from it and leave buy at zero until the next repricing run
                # fills it in -- inventing a buy price we never observed would
                # be worse than admitting we do not have one yet.
                """INSERT INTO prices (type_id, buy_price, sell_price, source, samples, updated_at)
                   SELECT type_id, 0, price,
                          CASE WHEN source='jita_sell' THEN 'jita' ELSE source END,
                          samples, updated_at
                   FROM {old}""",
            )
            done.append("prices: split into buy_price/sell_price (table rebuilt)")

    # v1 -> v2: item-valued snapshot buckets gained a second basis.
    #
    # Same reasoning as the prices table above: trigger on the legacy column
    # being present, not on the new ones being absent. This table's old
    # columns had DEFAULT 0, so a database stuck in the intermediate state
    # (both assets_isk and assets_buy_isk present at once) never actually
    # crashed on it the way prices did -- but it would sit there with four
    # permanently-dead columns, silently drifting out of sync with what
    # SCHEMA says this table looks like. Same bug, just a quieter symptom.
    if _table_exists(conn, "networth_snapshots"):
        cols = columns(conn, "networth_snapshots")
        if "assets_isk" in cols:
            _rebuild_table(
                conn,
                "networth_snapshots",
                """CREATE TABLE networth_snapshots (
                       snapshot_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                       taken_at           TEXT NOT NULL,
                       owner_type         TEXT NOT NULL,
                       owner_id           INTEGER NOT NULL,
                       assets_buy_isk     REAL NOT NULL DEFAULT 0,
                       assets_sell_isk    REAL NOT NULL DEFAULT 0,
                       wallet_isk         REAL NOT NULL DEFAULT 0,
                       orders_isk         REAL NOT NULL DEFAULT 0,
                       escrow_isk         REAL NOT NULL DEFAULT 0,
                       contracts_buy_isk  REAL NOT NULL DEFAULT 0,
                       contracts_sell_isk REAL NOT NULL DEFAULT 0,
                       jobs_buy_isk       REAL NOT NULL DEFAULT 0,
                       jobs_sell_isk      REAL NOT NULL DEFAULT 0,
                       total_buy_isk      REAL NOT NULL DEFAULT 0,
                       total_sell_isk     REAL NOT NULL DEFAULT 0
                   )""",
                # Old snapshots were taken at Jita sell -- copy them into the
                # sell columns so the chart keeps its shape, and leave buy at
                # zero rather than inventing a spread for history we cannot
                # reconstruct. snapshot_id is carried over explicitly so
                # nothing that already referenced a specific snapshot breaks.
                """INSERT INTO networth_snapshots
                       (snapshot_id, taken_at, owner_type, owner_id,
                        assets_buy_isk, assets_sell_isk, wallet_isk, orders_isk, escrow_isk,
                        contracts_buy_isk, contracts_sell_isk, jobs_buy_isk, jobs_sell_isk,
                        total_buy_isk, total_sell_isk)
                   SELECT snapshot_id, taken_at, owner_type, owner_id,
                          0, assets_isk, wallet_isk, orders_isk, escrow_isk,
                          0, contracts_isk, 0, jobs_isk,
                          0, total_isk
                   FROM {old}""",
            )
            done.append("networth_snapshots: added buy/sell columns (table rebuilt)")

    if done:
        set_meta(conn, "migrated_at", str(SCHEMA_VERSION))
    return done


def init(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    migrate(conn)
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def upsert_many(
    conn: sqlite3.Connection, table: str, columns: list[str], rows: Iterable[tuple]
) -> int:
    placeholders = ",".join("?" * len(columns))
    cols = ",".join(columns)
    sql = f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})"
    cur = conn.executemany(sql, rows)
    return cur.rowcount
