"""SQLite storage: schema, migrations, connection helper."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH

SCHEMA_VERSION = 5

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
    published       INTEGER NOT NULL DEFAULT 1,
    -- types.isDynamicType: true on exactly the 89 mutated ("Abyssal ...")
    -- module and drone types, and the only safe gate for asking ESI's
    -- dynamic-item route about an asset. meta_group_id = 15 looks like the
    -- same thing but is not: it also covers 170 mutaplasmids and a
    -- blueprint, and every one of those would 404 and burn error budget.
    is_dynamic_type INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_types_name  ON sde_types(name);
CREATE INDEX IF NOT EXISTS idx_types_group ON sde_types(group_id);

-- ------------------------------------------------------------- dogma (SDE)
-- Only what rendering an abyssal item's rolls needs. Attribute names and
-- units come from dogmaAttributes/dogmaUnits; the un-mutated base values
-- from typeDogma, restricted at import to the source and abyssal types a
-- mutaplasmid can touch (a few hundred types, not the whole 26k); the roll
-- ranges from dynamicItemAttributes, keyed by the mutaplasmid because
-- several mutaplasmids with different ranges produce the same abyssal type.
CREATE TABLE IF NOT EXISTS sde_dogma_attributes (
    attribute_id  INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    display_name  TEXT,
    unit_id       INTEGER,
    high_is_good  INTEGER,
    default_value REAL,
    published     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_dogma_attr_display ON sde_dogma_attributes(display_name);

CREATE TABLE IF NOT EXISTS sde_dogma_units (
    unit_id      INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    display_name TEXT
);

CREATE TABLE IF NOT EXISTS sde_type_dogma (
    type_id      INTEGER NOT NULL,
    attribute_id INTEGER NOT NULL,
    value        REAL NOT NULL,
    PRIMARY KEY (type_id, attribute_id)
);

-- high_is_good is the per-mutaplasmid polarity override CCP added in build
-- 3030547; NULL means "use the attribute's own highIsGood". It is what makes
-- a webifier's speedFactor read as low-is-good while an afterburner's reads
-- high-is-good, so it must stay nullable rather than defaulting.
CREATE TABLE IF NOT EXISTS sde_mutator_ranges (
    mutator_type_id   INTEGER NOT NULL,
    attribute_id      INTEGER NOT NULL,
    min_mult          REAL NOT NULL,
    max_mult          REAL NOT NULL,
    high_is_good      INTEGER,
    resulting_type_id INTEGER,
    PRIMARY KEY (mutator_type_id, attribute_id)
);

-- ------------------------------------------------------------ abyssal rolls
-- One row per mutated item ever asked about, keyed by item_id alone: rolls
-- are permanent (CCP patch notes, 2018-05-25) and an item keeps its id when
-- it changes hands, so the row is not owner-scoped and never expires.
-- status 'missing' records a 404 so the item is not re-asked on every run --
-- each 4xx costs one unit of the 100-per-minute error budget.
CREATE TABLE IF NOT EXISTS abyssal_items (
    item_id         INTEGER PRIMARY KEY,
    type_id         INTEGER NOT NULL,
    source_type_id  INTEGER,
    mutator_type_id INTEGER,
    created_by      INTEGER,
    status          TEXT NOT NULL,      -- 'ok' | 'missing'
    fetched_at      TEXT NOT NULL
);

-- The item's FULL dogma_attributes list as ESI returns it. Which of them
-- were rolled is decided at query time from the mutator's range table, so
-- a future SDE with a new mutable attribute needs no re-fetch.
CREATE TABLE IF NOT EXISTS abyssal_attributes (
    item_id      INTEGER NOT NULL REFERENCES abyssal_items(item_id) ON DELETE CASCADE,
    attribute_id INTEGER NOT NULL,
    value        REAL NOT NULL,
    PRIMARY KEY (item_id, attribute_id)
);

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
--
-- Two kinds of row live here. Anything seen through /universe/structures --
-- somebody else's Astrahus your ship is parked in -- fills in the name and
-- location and nothing else. Structures your own corporation owns come from
-- /corporations/{id}/structures and carry the operational columns below,
-- which is everything the Structures tab is about. owned tells them apart.
--
-- Timestamps are stored exactly as ESI sends them: ISO 8601, UTC. EVE runs
-- on UTC and every timer in the game is quoted in it, so converting to local
-- time on the way in would mean converting back before showing anyone a
-- number they are expected to form up on.
CREATE TABLE IF NOT EXISTS structures (
    structure_id         INTEGER PRIMARY KEY,
    name                 TEXT,
    system_id            INTEGER,
    region_id            INTEGER,
    type_id              INTEGER,
    owner_id             INTEGER,
    resolved_at          TEXT,
    accessible           INTEGER NOT NULL DEFAULT 1,
    owned                INTEGER NOT NULL DEFAULT 0,
    state                TEXT,
    state_timer_start    TEXT,
    state_timer_end      TEXT,
    fuel_expires         TEXT,
    reinforce_hour       INTEGER,
    next_reinforce_hour  INTEGER,
    next_reinforce_apply TEXT,
    unanchors_at         TEXT,
    services             TEXT,
    updated_at           TEXT,
    -- Set when a structure stops being reported by ESI, which is what an
    -- unanchor looks like from out here: the row is kept rather than deleted
    -- because this table doubles as the name resolver for asset locations
    -- (see ASSET_ROWS), and dropping it would turn anything still recorded
    -- there into "Unknown location 1048...".
    gone_at              TEXT
);

-- Moon drill extractions, one row per drill. ESI only ever reports the
-- current cycle, so this is a snapshot rather than history.
CREATE TABLE IF NOT EXISTS moon_extractions (
    structure_id          INTEGER PRIMARY KEY,
    moon_id               INTEGER,
    owner_id              INTEGER,
    extraction_start_time TEXT,
    chunk_arrival_time    TEXT,
    natural_decay_time    TEXT,
    updated_at            TEXT NOT NULL
);

-- ----------------------------------------------------------------- stockpiles
-- A stockpile is a list of things you want to keep on hand, plus a rule for
-- what counts towards them. The rule is deliberately narrower than
-- jEveAssets': owner and location, rather than an arbitrary filter tree.
-- Those two answer "keep 20 Damage Controls in Jita on this character",
-- which is what the feature is for.
--
-- Sources are opt-in per stockpile because what counts as "have" is a
-- judgement, not a fact. Items listed on the market are still yours and are
-- one click from being yours again; a job three days from delivery is not
-- something you can undock with. Neither answer is right for everyone, so
-- both are a checkbox with assets always counted.
CREATE TABLE IF NOT EXISTS stockpiles (
    stockpile_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    owner_type        TEXT,             -- NULL: any owner counts
    owner_id          INTEGER,
    location_scope    TEXT    NOT NULL DEFAULT 'any',  -- any|region|system|station
    location_id       INTEGER,
    multiplier        REAL    NOT NULL DEFAULT 1,
    include_orders    INTEGER NOT NULL DEFAULT 0,
    include_jobs      INTEGER NOT NULL DEFAULT 0,
    include_contracts INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS stockpile_items (
    stockpile_id INTEGER NOT NULL REFERENCES stockpiles(stockpile_id) ON DELETE CASCADE,
    type_id      INTEGER NOT NULL,
    target       REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (stockpile_id, type_id)
);

CREATE INDEX IF NOT EXISTS ix_stockpile_items_stockpile ON stockpile_items(stockpile_id);

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
-- item_id alone (the PK starts with owner_type, so it cannot serve this):
-- the fitted/loose clause correlates p.item_id = a.location_id, and without
-- this index the is:fitted chip was a full-scan-per-row query -- measured
-- 17 seconds over 25k stacks, 7 ms with the index.
CREATE INDEX IF NOT EXISTS idx_assets_item ON assets(item_id);

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

-- ------------------------------------------------------------ assets tab UI
-- Rail pins and saved omnibox views live in the database, not settings.json:
-- they reference data (labels, filter grammar) rather than preferences, and
-- keeping them beside the assets means a copied database carries them along.
-- Brand new tables, so CREATE TABLE IF NOT EXISTS is the whole migration.
CREATE TABLE IF NOT EXISTS pinned_labels (
    level TEXT NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (level, label)
);

-- state_json is the omnibox spec plus whatever view state the Assets tab
-- chooses to remember (group-by, rail level); the schema stays agnostic so
-- the UI can grow the payload without a migration.
CREATE TABLE IF NOT EXISTS saved_views (
    slot       INTEGER PRIMARY KEY,
    state_json TEXT NOT NULL
);

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

    # v2 -> v3: structures went from a name lookup table to the backing store
    # for the Structures tab.
    #
    # The rows already there are worth keeping -- they are what stops the app
    # re-asking ESI for the name of every structure it has ever seen, including
    # the ones it is not allowed to dock in -- so this rebuilds rather than
    # starting fresh. Everything added is nullable: an existing row has no
    # operational data and will not have any until the next corp sync, and
    # owned defaults to 0 so a structure that was only ever a name lookup does
    # not claim to be one of ours.
    if _table_exists(conn, "structures") and "state" not in columns(conn, "structures"):
        _rebuild_table(
            conn,
            "structures",
            """CREATE TABLE structures (
                   structure_id         INTEGER PRIMARY KEY,
                   name                 TEXT,
                   system_id            INTEGER,
                   region_id            INTEGER,
                   type_id              INTEGER,
                   owner_id             INTEGER,
                   resolved_at          TEXT,
                   accessible           INTEGER NOT NULL DEFAULT 1,
                   owned                INTEGER NOT NULL DEFAULT 0,
                   state                TEXT,
                   state_timer_start    TEXT,
                   state_timer_end      TEXT,
                   fuel_expires         TEXT,
                   reinforce_hour       INTEGER,
                   next_reinforce_hour  INTEGER,
                   next_reinforce_apply TEXT,
                   unanchors_at         TEXT,
                   services             TEXT,
                   updated_at           TEXT
               )""",
            """INSERT INTO structures
                   (structure_id, name, system_id, region_id, type_id, owner_id,
                    resolved_at, accessible)
               SELECT structure_id, name, system_id, region_id, type_id, owner_id,
                      resolved_at, accessible
               FROM {old}""",
        )
        done.append("structures: added state, fuel and timer columns (table rebuilt)")

    # v3 -> v4: mark structures ESI has stopped reporting (an unanchor) rather
    # than leaving them in the list with a frozen state and a fuel clock that
    # keeps counting down to a date nothing will ever refresh. A plain ALTER
    # is enough here where the block above needed a rebuild: the column is
    # nullable with no default, so existing rows are simply NULL -- "still
    # here as far as we know", which is the right answer until the next sync.
    if _table_exists(conn, "structures") and "gone_at" not in columns(conn, "structures"):
        conn.execute("ALTER TABLE structures ADD COLUMN gone_at TEXT")
        done.append("structures: added gone_at")

    # v4 -> v5: sde_types gained is_dynamic_type for the abyssal-stats gate.
    #
    # This one triggers on the new column being absent, which is the opposite
    # of the rule the rebuild migrations above were burned by -- deliberately.
    # Those triggered on a legacy column because an intermediate state (both
    # old and new columns present) had shipped and had to be repaired; here
    # nothing is removed, so there is no legacy column to key on and no
    # half-migrated shape to distinguish. The rebuild rather than ADD COLUMN
    # keeps the table byte-for-byte in SCHEMA's shape, including NOT NULL
    # DEFAULT 0, and existing rows honestly read 0 until the next SDE import
    # (which sde.tables_stale forces at startup) fills the flag in.
    if _table_exists(conn, "sde_types") and "is_dynamic_type" not in columns(conn, "sde_types"):
        _rebuild_table(
            conn,
            "sde_types",
            """CREATE TABLE sde_types (
                   type_id         INTEGER PRIMARY KEY,
                   name            TEXT NOT NULL,
                   group_id        INTEGER,
                   market_group_id INTEGER,
                   meta_group_id   INTEGER,
                   volume          REAL,
                   capacity        REAL,
                   portion_size    INTEGER,
                   base_price      REAL,
                   published       INTEGER NOT NULL DEFAULT 1,
                   is_dynamic_type INTEGER NOT NULL DEFAULT 0
               )""",
            """INSERT INTO sde_types
                   (type_id, name, group_id, market_group_id, meta_group_id, volume,
                    capacity, portion_size, base_price, published)
               SELECT type_id, name, group_id, market_group_id, meta_group_id, volume,
                      capacity, portion_size, base_price, published
               FROM {old}""",
        )
        # The indexes followed the renamed table into the DROP; recreate them
        # or every name lookup is a full scan until the next init().
        conn.execute("CREATE INDEX IF NOT EXISTS idx_types_name  ON sde_types(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_types_group ON sde_types(group_id)")
        done.append("sde_types: added is_dynamic_type (table rebuilt)")

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
