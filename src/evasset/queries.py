"""Read-side SQL for the UI. Kept apart from the sync code on purpose."""

from __future__ import annotations

import sqlite3

from . import abyssal
from .config import ASSET_SAFETY_LOCATION_ID

# The station/structure/system a root-level item is sitting in, or the fixed
# "Asset Safety" label for anything CCP repackaged after an eviction/pod loss
# (see ASSET_SAFETY_LOCATION_ID -- a fixed constant, not a real place, so it
# never resolves to a station/structure/system name on its own). Shared
# between ASSET_ROWS itself and OVERVIEW_FILTER_EXPR below so the label a
# user clicks in the rail (or picks from a completion) is the exact same
# expression the table is then filtered on -- no risk of the two disagreeing
# on what a location is "called".
LOCATION_EXPR = f"""CASE WHEN a.root_location_id = {ASSET_SAFETY_LOCATION_ID} THEN 'Asset Safety'
         ELSE COALESCE(st.name, sr.name, rsys.name,
                        'Unknown location ' || a.root_location_id)
    END"""

# Every asset row, already joined to names, location and price.
ASSET_ROWS = f"""
WITH RECURSIVE
-- Every ancestor of a row that is itself an asset, outermost first, as
-- "Asset Safety Wrap > L2 HMS Dragoon". The station is deliberately not in it
-- -- that is the Location column, and repeating it on every row would spend
-- width saying what is already said.
--
-- A path rather than just the immediate parent, because the nesting is real:
-- a fuel can in a capital's hangar, a can in a corp division, a ship inside an
-- asset safety wrap. Naming only the innermost container answers "which can"
-- while losing "which ship", and those are the same question asked twice.
--
-- The Slot column stays, and stays complementary. It carries the compartment
-- when that is a property of the row itself (CorpSAG1 for a corp division,
-- HiSlot0 for a fitting). This carries it when the compartment is a property
-- of an ancestor -- an item in an asset safety wrap has its own flag set to
-- Hangar, because it is in the hangar of the wrap.
--
-- Depth is capped rather than trusted. Container cycles should be impossible,
-- but the flattening resolver already had to be hardened against one, and an
-- unbounded recursive CTE meeting a cycle does not return.
-- Owner is carried through the walk, not just item_id. The assets primary
-- key is (owner_type, owner_id, item_id), and following it means a container
-- is only ever matched within its own owner's assets -- and that the join uses
-- that index rather than scanning.
container_walk(owner_type, owner_id, item_id, parent_id, depth, path) AS (
    SELECT a.owner_type, a.owner_id, a.item_id, a.location_id, 0, ''
    FROM assets a
    UNION ALL
    SELECT w.owner_type, w.owner_id, w.item_id, par.location_id, w.depth + 1,
           COALESCE(par.custom_name, pt.name)
           || CASE WHEN w.path = '' THEN '' ELSE ' > ' || w.path END
    FROM container_walk w
    JOIN assets par ON par.owner_type = w.owner_type
                   AND par.owner_id   = w.owner_id
                   AND par.item_id    = w.parent_id
    LEFT JOIN sde_types pt ON pt.type_id = par.type_id
    WHERE w.depth < 8
),
-- The deepest row per item is the complete path; the shallower ones are its
-- prefixes. Picked with a window function rather than a correlated
-- MAX(depth) subselect, which EXPLAIN QUERY PLAN showed running once per
-- asset row -- the exact shape the abyssal roll clause has a test guarding
-- against, and for the same reason.
container_path(owner_type, owner_id, item_id, path) AS (
    SELECT owner_type, owner_id, item_id, path FROM (
        SELECT owner_type, owner_id, item_id, path,
               ROW_NUMBER() OVER (
                   PARTITION BY owner_type, owner_id, item_id ORDER BY depth DESC
               ) AS rank
        FROM container_walk WHERE path <> ''
    ) WHERE rank = 1
)
SELECT
    a.owner_type,
    a.owner_id,
    COALESCE(ch.name, co.name, a.owner_type || ' ' || a.owner_id)          AS owner,
    a.item_id,
    a.type_id,
    t.name                                                                 AS item,
    a.custom_name,
    g.name                                                                 AS grp,
    cat.name                                                               AS category,
    mg.name                                                                AS meta,
    a.quantity,
    a.location_flag,
    -- What the row is directly inside: a can, a ship, an asset safety wrap.
    -- NULL when it sits in the location itself, so the column is blank for
    -- the common case rather than repeating the station on every row.
    --
    -- The Slot column answers the same question wherever the compartment is a
    -- property of the row (CorpSAG1 for a corp division, HiSlot0 for a
    -- fitting). It cannot answer it when the compartment is a property of the
    -- parent: an item inside an asset safety wrap has its own flag set to
    -- Hangar, because it is in the hangar of the wrap. This is that case.
    --
    -- The container's custom name wins: "in Ore Can 3" beats "in Station
    -- Container" for anyone trying to find the thing again.
    cp.path                                                                AS container,
    a.is_singleton,
    a.is_blueprint_copy,
    t.is_dynamic_type                                                      AS is_dynamic_type,
    a.root_location_id,
    {LOCATION_EXPR}                                                        AS location,
    sys.name                                                               AS system,
    sys.security                                                           AS security,
    reg.name                                                               AS region,
    COALESCE(p.buy_price, 0)                                               AS buy_price,
    COALESCE(p.sell_price, 0)                                              AS sell_price,
    COALESCE(p.source, 'none')                                             AS price_source,
    p.updated_at                                                           AS price_updated_at,
    a.quantity * COALESCE(p.buy_price, 0)                                  AS buy_value,
    a.quantity * COALESCE(p.sell_price, 0)                                 AS sell_value,
    COALESCE(t.volume, 0)                                                  AS unit_volume,
    a.quantity * COALESCE(t.volume, 0)                                     AS volume
FROM assets a
JOIN      sde_types      t    ON t.type_id      = a.type_id
LEFT JOIN container_path cp   ON cp.owner_type  = a.owner_type
                             AND cp.owner_id    = a.owner_id
                             AND cp.item_id     = a.item_id
LEFT JOIN sde_groups     g    ON g.group_id     = t.group_id
LEFT JOIN sde_categories cat  ON cat.category_id = g.category_id
LEFT JOIN sde_meta_groups mg  ON mg.meta_group_id = t.meta_group_id
LEFT JOIN prices         p    ON p.type_id      = a.type_id
LEFT JOIN sde_stations   st   ON st.station_id  = a.root_location_id
LEFT JOIN structures     sr   ON sr.structure_id = a.root_location_id
LEFT JOIN sde_systems    rsys ON rsys.system_id = a.root_location_id
LEFT JOIN sde_systems    sys  ON sys.system_id  = a.system_id
LEFT JOIN sde_regions    reg  ON reg.region_id  = a.region_id
LEFT JOIN characters     ch   ON a.owner_type = 'character'   AND ch.character_id   = a.owner_id
LEFT JOIN corporations   co   ON a.owner_type = 'corporation' AND co.corporation_id = a.owner_id
"""

ASSET_COLUMNS = [
    ("owner", "Owner"),
    ("item", "Item"),
    ("custom_name", "Name"),
    ("quantity", "Qty"),
    ("grp", "Group"),
    ("category", "Category"),
    ("meta", "Meta"),
    ("location", "Location"),
    ("container", "Container"),
    ("location_flag", "Slot"),
    ("system", "System"),
    ("region", "Region"),
    ("buy_value", "Buy value"),
    ("sell_value", "Sell value"),
    ("price_source", "Price from"),
    ("volume", "Volume m3"),
]

NUMERIC_COLUMNS = {
    "quantity", "volume", "unit_volume", "security",
    "buy_price", "sell_price", "buy_value", "sell_value",
}
ISK_COLUMNS = {"buy_price", "sell_price", "buy_value", "sell_value"}

# (display label, level key) for every way the assets can be rolled up. The
# shared vocabulary between the UI that offers the levels -- the rail's level
# combo, the Assets group-by, the Treemap tab -- and the SQL that implements
# them: the UI shows the labels, and the keys index OVERVIEW_FILTER_EXPR and
# _LEVEL_COLUMN below. A level added here without a matching entry in each is
# caught immediately rather than drifting apart per layer.
ROLLUP_LEVELS = [
    ("Location", "location"),
    ("Solar system", "system"),
    ("Region", "region"),
    ("Owner", "owner"),
    ("Category", "category"),
    ("Group", "group"),
]

# One WHERE-usable expression per grouping level in ROLLUP_LEVELS, so an
# exact-label chip ("Jita IV - Moon 4 ...", "Asset Safety", a region, an
# owner, ...) filters on the very expression the rollups group by -- see
# omni.py, which builds its clauses from this table. Keyed the same as
# ROLLUP_LEVELS' second element and _LEVEL_COLUMN below.
OVERVIEW_FILTER_EXPR = {
    "location": LOCATION_EXPR,
    "system": "sys.name",
    "region": "reg.name",
    "owner": "COALESCE(ch.name, co.name, a.owner_type || ' ' || a.owner_id)",
    "category": "cat.name",
    "group": "g.name",
}

# True for a row whose *direct* parent (a.location_id) is itself something
# the SDE calls a Ship -- i.e. anything fitted, in cargo, in the drone bay,
# in the fleet hangar, or in any other ship hold. Deliberately the same
# "everything sitting on this ship" scope as FIT_ROWS below, just inverted
# and correlated instead of parameterized by one ship's item_id: the point
# here is "hide whatever is not loose in a station/structure hangar",
# regardless of which ship it is sitting on.
HIDE_SHIP_CONTENTS_CLAUSE = """NOT EXISTS (
    SELECT 1 FROM assets p
    JOIN sde_types      pt ON pt.type_id      = p.type_id
    JOIN sde_groups     pg ON pg.group_id     = pt.group_id
    JOIN sde_categories pc ON pc.category_id  = pg.category_id
    WHERE p.item_id = a.location_id AND pc.name = 'Ship'
)"""


def fetch_assets(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> list[sqlite3.Row]:
    sql = ASSET_ROWS + (f" WHERE {where}" if where else "")
    return list(conn.execute(sql, params))


def count_assets(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> int:
    """How many rows fetch_assets would return for the same WHERE.

    The abyssal card's live "N of TOTAL match" asks this after every handle
    move, so it is a COUNT over the same joins rather than a fetch whose
    rows are thrown away: the answer is one integer, and the fetch would
    build the owner, station and price columns of every matching row only
    to discard them. Wrapping ASSET_ROWS whole, rather than rewriting its
    joins, keeps the count honest against the table -- the WHERE is the
    same one the omnibox hands fetch_assets.
    """
    sql = f"SELECT COUNT(*) FROM ({ASSET_ROWS}{f' WHERE {where}' if where else ''})"
    return int(conn.execute(sql, params).fetchone()[0])


# Everything sitting directly on a ship: fitted modules and charges, drones,
# fighters, cargo, fleet hangar, and any of the specialized holds -- anything
# whose location_id is that ship's item_id. Deliberately not filtered to a
# "fitting" subset of location_flag values here; grouping/labelling that is
# the fit dialog's job (see ui/fit_dialog.py), and it has a fallback for any
# flag this query was not specifically told about, so nothing on the ship is
# ever silently left out of the SQL either.
FIT_ROWS = """
SELECT
    a.item_id,
    a.type_id,
    t.name       AS item,
    t.meta_group_id,
    a.custom_name,
    a.quantity,
    a.location_flag,
    cat.name     AS category,
    a.is_blueprint_copy
FROM assets a
JOIN      sde_types      t   ON t.type_id       = a.type_id
LEFT JOIN sde_groups     g   ON g.group_id      = t.group_id
LEFT JOIN sde_categories cat ON cat.category_id = g.category_id
WHERE a.location_id = ?
ORDER BY a.location_flag, t.name
"""


def fetch_fit(conn: sqlite3.Connection, ship_item_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(FIT_ROWS, (ship_item_id,)))


def group_names(
    conn: sqlite3.Connection, level: str, where: str = "", params: tuple = ()
) -> list[str]:
    """Distinct labels for one grouping level, faceted by the current filters.

    Faceted on purpose: a picker fed from here takes the same WHERE the table
    is showing, so it only ever lists names that still have rows behind them
    -- picking one always narrows to something visible instead of an empty
    table. Names only, no totals: this is a filter vocabulary, not a report,
    and the table underneath already sums whatever the pick leaves. The WHERE
    is written against ASSET_ROWS' inner aliases (t.name,
    a.root_location_id, ...), so it must be injected inside the subquery, not
    around it.
    """
    col = _LEVEL_COLUMN.get(level, "location")
    sql = f"""
        SELECT DISTINCT {col} AS label
        FROM ({ASSET_ROWS} {f"WHERE {where}" if where else ""})
        WHERE label IS NOT NULL
        ORDER BY label COLLATE NOCASE
    """
    return [r["label"] for r in conn.execute(sql, params)]


# Level key -> ASSET_ROWS output column, shared by group_names, rail_rollups
# and where_is_item so all three agree on what each level is labelled by.
# "group" maps to "grp" because ASSET_ROWS has to dodge the SQL keyword.
_LEVEL_COLUMN = {
    "location": "location",
    "system": "system",
    "region": "region",
    "owner": "owner",
    "category": "category",
    "group": "grp",
}

_RAIL_ORDER = {
    "value": "sell_value DESC",
    "name": "label COLLATE NOCASE ASC",
    "volume": "volume DESC",
}


def rail_rollups(
    conn: sqlite3.Connection,
    level: str,
    where: str = "",
    params: tuple = (),
    sort: str = "value",
) -> list[sqlite3.Row]:
    """Per-label rollups: label, stacks, units, volume, buy_value, sell_value.

    Feeds the side rail and the Treemap tab. buy_value is carried alongside
    sell_value because the treemap lets you size tiles by either basis; the
    rail only reads sell_value, and one extra SUM over an already-grouped
    scan is cheaper than a second near-identical query.

    Faceted the same way as group_names -- the WHERE is written against
    ASSET_ROWS' inner aliases and injected inside the subquery -- so the rail
    only ever offers labels that still have rows behind them under the
    current filters. NULL labels (an unresolved system mid-sync, a missing
    meta group) are dropped rather than rendered as a blank unclickable row.
    An unknown sort falls back to value: the sort key crosses the UI boundary
    as a plain string, and a typo there should degrade, not raise.
    """
    col = _LEVEL_COLUMN.get(level, "location")
    order = _RAIL_ORDER.get(sort, _RAIL_ORDER["value"])
    sql = f"""
        SELECT {col}         AS label,
               COUNT(*)      AS stacks,
               SUM(quantity) AS units,
               SUM(volume)   AS volume,
               SUM(buy_value)  AS buy_value,
               SUM(sell_value) AS sell_value
        FROM ({ASSET_ROWS} {f"WHERE {where}" if where else ""})
        WHERE label IS NOT NULL
        GROUP BY label
        ORDER BY {order}
    """
    return list(conn.execute(sql, params))


def where_is_item(
    conn: sqlite3.Connection, level: str, where: str = "", params: tuple = ()
) -> list[sqlite3.Row]:
    """Total quantity per label -- the rail's "where is it" flip.

    When the user is hunting one item, per-label ISK rollups answer the wrong
    question; what matters is how many of the thing sit at each place, biggest
    pile first. Same faceting and NULL handling as rail_rollups.
    """
    col = _LEVEL_COLUMN.get(level, "location")
    sql = f"""
        SELECT {col} AS label, SUM(quantity) AS quantity
        FROM ({ASSET_ROWS} {f"WHERE {where}" if where else ""})
        WHERE label IS NOT NULL
        GROUP BY label
        ORDER BY quantity DESC
    """
    return list(conn.execute(sql, params))


def _enabled_owner_sql(alias: str) -> str:
    """WHERE fragment keeping only rows that belong to a counted owner.

    Mirrors networth.compute_all's roster exactly: characters flagged
    enabled, plus corporations that have a token to be synced through
    (via_character_id set). Data for a disabled character stays in the
    database -- disabling is "stop counting this", not "forget this" -- so
    every whole-estate figure must apply the same roster, or the strip and
    the Net worth tab disagree over the same file the moment a character is
    switched off.
    """
    return f"""(({alias}.owner_type = 'character' AND {alias}.owner_id IN
             (SELECT character_id FROM characters WHERE enabled = 1))
        OR ({alias}.owner_type = 'corporation' AND {alias}.owner_id IN
             (SELECT corporation_id FROM corporations WHERE via_character_id IS NOT NULL)))"""


def estate_summary(conn: sqlite3.Connection) -> dict:
    """Whole-estate figures for the net-worth strip.

    Whole-estate means every enabled owner, and deliberately not the table's
    current filters: the strip answers "what is everything worth" while the
    table underneath answers "what am I looking at", and tying the strip to
    the filters would make the headline number jump around every time a chip
    is added. Assets are valued at Jita sell only -- the strip is a glance,
    not the two-basis report the Net worth tab already provides.
    wallet_liquid is the same liquid bucket networth.py computes per owner
    (balances, plus sell orders at their listed price -- the ISK actually
    coming back if they fill -- plus buy-order escrow), summed over the same
    enabled-owner roster so the two screens always agree.
    """
    row = conn.execute(
        f"""SELECT SUM(sell_value) AS assets_sell,
                   SUM(volume)     AS volume,
                   SUM(CASE WHEN price_source = 'none' THEN 1 ELSE 0 END) AS unpriced_stacks
            FROM ({ASSET_ROWS} WHERE {_enabled_owner_sql("a")})"""
    ).fetchone()
    assets_sell = float(row["assets_sell"] or 0)
    liquid = conn.execute(
        f"""SELECT COALESCE((SELECT SUM(balance) FROM wallets w
                             WHERE {_enabled_owner_sql("w")}), 0)
                 + COALESCE((SELECT SUM(volume_remain * price) FROM market_orders o
                             WHERE o.is_buy_order = 0 AND {_enabled_owner_sql("o")}), 0)
                 + COALESCE((SELECT SUM(escrow) FROM market_orders o
                             WHERE o.is_buy_order = 1 AND {_enabled_owner_sql("o")}), 0)"""
    ).fetchone()[0]
    return {
        "assets_sell": assets_sell,
        "wallet_liquid": float(liquid or 0),
        "total": assets_sell + float(liquid or 0),
        "volume": float(row["volume"] or 0),
        "unpriced_stacks": int(row["unpriced_stacks"] or 0),
    }


def value_map(conn: sqlite3.Connection, limit: int = 6) -> list[sqlite3.Row]:
    """Top locations estate-wide by sell value: label, sell_value.

    Feeds the strip's one-row value map: same enabled-owner roster as
    estate_summary (the segments must sum to what the strip's headline
    counts), unfiltered like the rest of the strip. The limit only bounds
    the query -- the widget itself culls to what its width can show and
    folds the rest into a residue segment (see ui/strip.py).
    """
    sql = f"""
        SELECT location AS label, SUM(sell_value) AS sell_value
        FROM ({ASSET_ROWS} WHERE {_enabled_owner_sql("a")})
        WHERE label IS NOT NULL
        GROUP BY label
        ORDER BY sell_value DESC
        LIMIT {int(limit)}
    """
    return list(conn.execute(sql))


def price_coverage(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        f"""SELECT
              COUNT(*)                                                     AS rows,
              SUM(CASE WHEN price_source='none' THEN 1 ELSE 0 END)          AS unpriced,
              SUM(CASE WHEN price_source='base_price' THEN 1 ELSE 0 END)    AS base_priced,
              SUM(CASE WHEN price_source='contract_avg' THEN 1 ELSE 0 END)  AS contract_priced,
              SUM(buy_value)                                                AS total_buy,
              SUM(sell_value)                                               AS total_sell
            FROM ({ASSET_ROWS})"""
    ).fetchone()
    return dict(row) if row else {}


def search_clause(text: str) -> tuple[str, tuple]:
    """Free text over item name, custom name, location, system, region, owner."""
    text = (text or "").strip()
    if not text:
        return "", ()
    terms = [t for t in text.split() if t]
    clauses, params = [], []
    for t in terms:
        like = f"%{t}%"
        clauses.append(
            "(t.name LIKE ? OR a.custom_name LIKE ? OR g.name LIKE ? OR cat.name LIKE ? "
            "OR COALESCE(st.name, sr.name, rsys.name) LIKE ? OR sys.name LIKE ? "
            "OR reg.name LIKE ? OR COALESCE(ch.name, co.name) LIKE ?)"
        )
        params.extend([like] * 8)
    return " AND ".join(clauses), tuple(params)


# ------------------------------------------------------------ wallet history
JOURNAL_ROWS = """
SELECT
    j.date,
    COALESCE(ch.name, co.name, j.owner_type || ' ' || j.owner_id) AS owner,
    j.division,
    j.ref_type,
    j.amount,
    j.balance,
    j.description,
    j.reason,
    -- Prefer a name we already hold locally (it is one of your own characters
    -- or corps) over the /universe/names cache, and fall back to the raw id.
    COALESCE(c1.name, k1.name, n1.name, CAST(j.first_party_id  AS TEXT)) AS first_party,
    COALESCE(c2.name, k2.name, n2.name, CAST(j.second_party_id AS TEXT)) AS second_party,
    j.tax,
    j.owner_type,
    j.owner_id
FROM wallet_journal j
LEFT JOIN names        n1 ON n1.id = j.first_party_id
LEFT JOIN names        n2 ON n2.id = j.second_party_id
LEFT JOIN characters   c1 ON c1.character_id = j.first_party_id
LEFT JOIN characters   c2 ON c2.character_id = j.second_party_id
LEFT JOIN corporations k1 ON k1.corporation_id = j.first_party_id
LEFT JOIN corporations k2 ON k2.corporation_id = j.second_party_id
LEFT JOIN characters   ch ON j.owner_type = 'character'   AND ch.character_id   = j.owner_id
LEFT JOIN corporations co ON j.owner_type = 'corporation' AND co.corporation_id = j.owner_id
"""

JOURNAL_COLUMNS = [
    ("date", "Date"),
    ("owner", "Owner"),
    ("ref_type", "Type"),
    ("amount", "Amount"),
    ("balance", "Balance"),
    ("first_party", "From"),
    ("second_party", "To"),
    ("tax", "Tax"),
    ("description", "Description"),
    ("reason", "Reason"),
]

TRANSACTION_ROWS = """
SELECT
    t.date,
    COALESCE(ch.name, co.name, t.owner_type || ' ' || t.owner_id) AS owner,
    CASE WHEN t.is_buy THEN 'Buy' ELSE 'Sell' END                 AS side,
    ty.name                                                       AS item,
    t.quantity,
    t.unit_price,
    t.quantity * t.unit_price                                     AS value,
    COALESCE(st.name, sr.name, CAST(t.location_id AS TEXT))       AS location,
    COALESCE(n.name, CAST(t.client_id AS TEXT))                   AS client,
    t.owner_type,
    t.owner_id,
    t.type_id
FROM wallet_transactions t
LEFT JOIN sde_types    ty ON ty.type_id     = t.type_id
LEFT JOIN sde_stations st ON st.station_id  = t.location_id
LEFT JOIN structures   sr ON sr.structure_id = t.location_id
LEFT JOIN names        n  ON n.id           = t.client_id
LEFT JOIN characters   ch ON t.owner_type = 'character'   AND ch.character_id   = t.owner_id
LEFT JOIN corporations co ON t.owner_type = 'corporation' AND co.corporation_id = t.owner_id
"""

TRANSACTION_COLUMNS = [
    ("date", "Date"),
    ("owner", "Owner"),
    ("side", "Side"),
    ("item", "Item"),
    ("quantity", "Qty"),
    ("unit_price", "Unit price"),
    ("value", "Value"),
    ("location", "Location"),
    ("client", "Counterparty"),
]

NUMERIC_COLUMNS |= {"amount", "balance", "tax", "unit_price"}
ISK_COLUMNS |= {"amount", "balance", "tax", "unit_price"}
# Rendered as "2026-08-04 14:32" rather than the raw ISO string from ESI.
DATE_COLUMNS = {"date", "taken_at", "date_issued", "date_expired", "issued"}


def fetch_journal(
    conn: sqlite3.Connection, where: str = "", params: tuple = (), limit: int = 20000
) -> list[sqlite3.Row]:
    sql = JOURNAL_ROWS + (f" WHERE {where}" if where else "") + f" ORDER BY j.date DESC LIMIT {int(limit)}"
    return list(conn.execute(sql, params))


def fetch_transactions(
    conn: sqlite3.Connection, where: str = "", params: tuple = (), limit: int = 20000
) -> list[sqlite3.Row]:
    sql = (
        TRANSACTION_ROWS
        + (f" WHERE {where}" if where else "")
        + f" ORDER BY t.date DESC LIMIT {int(limit)}"
    )
    return list(conn.execute(sql, params))


def journal_search_clause(text: str) -> tuple[str, tuple]:
    text = (text or "").strip()
    if not text:
        return "", ()
    clauses, params = [], []
    for term in text.split():
        like = f"%{term}%"
        clauses.append(
            "(j.ref_type LIKE ? OR j.description LIKE ? OR j.reason LIKE ? "
            "OR COALESCE(c1.name, k1.name, n1.name) LIKE ? "
            "OR COALESCE(c2.name, k2.name, n2.name) LIKE ? "
            "OR COALESCE(ch.name, co.name) LIKE ?)"
        )
        params.extend([like] * 6)
    return " AND ".join(clauses), tuple(params)


def transaction_search_clause(text: str) -> tuple[str, tuple]:
    text = (text or "").strip()
    if not text:
        return "", ()
    clauses, params = [], []
    for term in text.split():
        like = f"%{term}%"
        clauses.append(
            "(ty.name LIKE ? OR COALESCE(st.name, sr.name) LIKE ? OR n.name LIKE ? "
            "OR COALESCE(ch.name, co.name) LIKE ?)"
        )
        params.extend([like] * 4)
    return " AND ".join(clauses), tuple(params)


def journal_ref_types(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT ref_type FROM wallet_journal WHERE ref_type IS NOT NULL "
            "ORDER BY ref_type"
        )
    ]


def trade_summary(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> dict:
    """Bought, sold and the difference over whatever is currently filtered.

    This is not profit. Matching a sale back to the lot it came from needs
    inventory accounting we do not do yet; this is cash in against cash out
    over the visible window.
    """
    sql = f"""
        SELECT
            SUM(CASE WHEN t.is_buy  THEN t.quantity * t.unit_price ELSE 0 END) AS bought,
            SUM(CASE WHEN t.is_buy  THEN 0 ELSE t.quantity * t.unit_price END) AS sold,
            COUNT(*)                                                           AS trades
        FROM wallet_transactions t
        LEFT JOIN sde_types    ty ON ty.type_id      = t.type_id
        LEFT JOIN sde_stations st ON st.station_id   = t.location_id
        LEFT JOIN structures   sr ON sr.structure_id = t.location_id
        LEFT JOIN names        n  ON n.id            = t.client_id
        LEFT JOIN characters   ch ON t.owner_type = 'character'   AND ch.character_id   = t.owner_id
        LEFT JOIN corporations co ON t.owner_type = 'corporation' AND co.corporation_id = t.owner_id
        {f"WHERE {where}" if where else ""}
    """
    row = conn.execute(sql, params).fetchone()
    bought = float(row["bought"] or 0)
    sold = float(row["sold"] or 0)
    return {"bought": bought, "sold": sold, "net": sold - bought, "trades": row["trades"] or 0}


def sde_build(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key='sde_build'").fetchone()
    return row["value"] if row else "not imported"


# ------------------------------------------------------------ abyssal rolls
def display_value_sql(value_expr: str, unit_expr: str) -> str:
    """The dogma value as the game displays it, for a given unit id.

    One CASE shared by the inspector's roll rows and the omnibox `stat:`
    filter, so a number the user reads off the screen is the number they
    can type into a comparison. The rules are dogmaUnits' own descriptions
    (build 3487903; docs/research/abyssal-stats.md section 5): 101 stores
    milliseconds and shows seconds; 108 and 111 store a resonance where
    0.0 means 100% and show (1 - v) * 100; 109 stores a multiplier around
    1.0 and shows (v - 1) * 100; 127 stores a 0..1 fraction and shows
    v * 100. Everything else, including the already-percent units 105, 121,
    124 and 205, is shown as stored. A NULL unit falls through to ELSE.
    Both expressions are SQL this module's callers own, never user input.
    """
    return f"""CASE {unit_expr}
        WHEN 101 THEN ({value_expr}) / 1000.0
        WHEN 108 THEN (1 - ({value_expr})) * 100
        WHEN 111 THEN (1 - ({value_expr})) * 100
        WHEN 109 THEN (({value_expr}) - 1) * 100
        WHEN 127 THEN ({value_expr}) * 100
        ELSE {value_expr}
    END"""


def roll_quality_sql(
    value: str,
    base: str,
    min_mult: str,
    max_mult: str,
    attr_high: str,
    mutator_high: str,
    attribute_id: str = "sd.attribute_id",
) -> str:
    """The roll's quality as a percent 0..100, or NULL, as one SQL expression.

    The SQL twin of abyssal.quality(abyssal.roll_position(...)) with the
    polarity of abyssal.resolve_polarity, so the omnibox `roll:` filter can
    rank every item inside the query instead of pulling every abyssal
    attribute row into Python and filtering there. The two are pinned equal
    over seeded rows in tests/test_abyssal.py; any change to one is a change
    to both. Arithmetic in the same order as the Python -- lo and hi are the
    MIN/MAX of base*min and base*max so a negative base still gives lo < hi,
    the position is clamped to 0..1, and NULL comes out when the base is
    unknown or the range degenerate. The one deliberate difference is the
    `* 1.0` before the division: a bound parameter has no column affinity,
    so two integer operands would divide as integers in SQLite where Python
    would not, and multiplying by 1.0 first is exact.

    Polarity is COALESCE(mutator, attribute, 1), the mutaplasmid's word
    first as in resolve_polarity. abyssal.POLARITY_OVERRIDES, when it holds
    anything, becomes a leading `CASE {attribute_id} WHEN id THEN flag ...`
    so the SQL and the Python keep agreeing the day an override lands; the
    ids and flags come from that module-level dict, never from user input.
    Read at call time, not import time, so a test can monkeypatch one in.

    Every argument is an SQL expression the caller owns. attribute_id is
    only consulted for the override CASE; it defaults to the alias the
    `roll:` filter uses.
    """
    lo = f"MIN(({base}) * ({min_mult}), ({base}) * ({max_mult}))"
    hi = f"MAX(({base}) * ({min_mult}), ({base}) * ({max_mult}))"
    pos = f"MIN(1.0, MAX(0.0, (({value}) - {lo}) * 1.0 / ({hi} - {lo})))"
    polarity = f"COALESCE({mutator_high}, {attr_high}, 1)"
    if abyssal.POLARITY_OVERRIDES:
        whens = " ".join(
            f"WHEN {int(aid)} THEN {1 if flag else 0}"
            for aid, flag in sorted(abyssal.POLARITY_OVERRIDES.items())
        )
        polarity = f"CASE {attribute_id} {whens} ELSE {polarity} END"
    return f"""CASE
        WHEN ({base}) IS NULL OR {hi} - {lo} <= 0 THEN NULL
        WHEN {polarity} = 1 THEN {pos} * 100
        ELSE (1 - {pos}) * 100
    END"""


# One stored attribute of one abyssal item, joined to everything needed to
# read it as a roll: name and unit, the source type's base (falling back to
# the attribute's SDE default when the source has no dogma row for it), and
# the mutator's range and polarity override. The source and mutator ids
# come from the item's own abyssal_items row, so the joins are bound rather
# than correlated. {rolled} is filled by fetch_abyssal_rolls with the rule
# for which attributes count as rolled -- see there.
_ABYSSAL_ROLL_ROWS = f"""
SELECT aa.attribute_id,
       da.name,
       COALESCE(da.display_name, da.name)                                   AS label,
       da.unit_id,
       du.display_name                                                      AS unit,
       aa.value                                                             AS raw_value,
       {display_value_sql("aa.value", "da.unit_id")}                        AS value,
       COALESCE(td.value, da.default_value)                                 AS raw_base,
       {display_value_sql("COALESCE(td.value, da.default_value)", "da.unit_id")} AS base,
       da.high_is_good                                                      AS attr_high_is_good,
       mr.high_is_good                                                      AS mutator_high_is_good,
       mr.min_mult,
       mr.max_mult,
       {display_value_sql("COALESCE(td.value, da.default_value) * mr.min_mult", "da.unit_id")}
                                                                            AS range_at_min,
       {display_value_sql("COALESCE(td.value, da.default_value) * mr.max_mult", "da.unit_id")}
                                                                            AS range_at_max
FROM abyssal_attributes aa
JOIN      sde_dogma_attributes da ON da.attribute_id = aa.attribute_id
LEFT JOIN sde_dogma_units      du ON du.unit_id = da.unit_id
LEFT JOIN sde_type_dogma       td ON td.type_id = :source AND td.attribute_id = aa.attribute_id
LEFT JOIN sde_mutator_ranges   mr ON mr.mutator_type_id = :mutator
                                 AND mr.attribute_id = aa.attribute_id
WHERE aa.item_id = :item AND ({{rolled}})
ORDER BY label COLLATE NOCASE, aa.attribute_id
"""

# The mutator's attribute set is the authoritative "what was rolled". When
# the SDE does not know the mutator (a brand-new mutaplasmid, or an SDE
# older than the item), the fallback is every attribute whose value differs
# from the base -- or whose base is unknown, since "unchanged" cannot then
# be shown either way.
_ROLLED_BY_MUTATOR = "mr.attribute_id IS NOT NULL"
_ROLLED_BY_DIFFERENCE = (
    "COALESCE(td.value, da.default_value) IS NULL "
    "OR COALESCE(td.value, da.default_value) <> aa.value"
)


def _roll_dict(r: sqlite3.Row) -> dict:
    high = abyssal.resolve_polarity(
        r["attribute_id"], r["attr_high_is_good"], r["mutator_high_is_good"]
    )
    position = abyssal.roll_position(r["raw_value"], r["raw_base"], r["min_mult"], r["max_mult"])
    # The range ends go through the same unit CASE as value and base, and are
    # ordered AFTER conversion: units 108 and 111 display (1 - v) * 100, so
    # the raw low end becomes the displayed high end. Sorting the raw pair
    # first (as roll_position does) and converting would hand the inspector
    # "Range: 15% to 4.60%" for the research's rate-of-fire roll. Unrankable
    # rolls carry no range at all rather than a pair no position could be
    # read against.
    lo = hi = None
    if position is not None:
        lo, hi = sorted((r["range_at_min"], r["range_at_max"]))
    return {
        "attribute_id": r["attribute_id"],
        "name": r["name"],
        "label": r["label"],
        "unit_id": r["unit_id"],
        "unit": r["unit"],
        "value": r["value"],
        "base": r["base"],
        "min": lo,
        "max": hi,
        "position": position,
        "quality": abyssal.quality(position, high),
        "high_is_good": high,
        "better": abyssal.verdict(r["raw_value"], r["raw_base"], high),
    }


def fetch_abyssal_rolls(conn: sqlite3.Connection, item_id: int) -> dict:
    """Everything the inspector shows about one abyssal item's rolls.

    status is 'unfetched' (no abyssal_items row yet), 'missing' (ESI
    answered 404) or 'ok'; source and mutator are type names, None when
    unknown. rolls carries the rolled attributes only, ordered by label:
    value, base, min and max are DISPLAY numbers (display_value_sql
    applied, so `duration` reads 9.0 seconds, not 9000), with min <= max in
    display terms and both None when the roll cannot be ranked; unit is the
    display symbol, position is the raw roll's place in the mutator's range
    and quality is that mirrored for low-is-good attributes -- quality is
    the one to draw a bar from, position is a datum. better is the verdict
    against the un-mutated source (None when equal, or when no base is
    known).
    """
    head = conn.execute(
        """SELECT i.status, i.source_type_id, i.mutator_type_id, i.created_by,
                  st.name AS source, mt.name AS mutator
           FROM abyssal_items i
           LEFT JOIN sde_types st ON st.type_id = i.source_type_id
           LEFT JOIN sde_types mt ON mt.type_id = i.mutator_type_id
           WHERE i.item_id = ?""",
        (item_id,),
    ).fetchone()
    if head is None:
        return {"status": abyssal.STATUS_UNFETCHED, "source": None, "mutator": None,
                "created_by": None, "rolls": []}
    out = {
        "status": head["status"],
        "source": head["source"],
        "mutator": head["mutator"],
        "created_by": head["created_by"],
        "rolls": [],
    }
    if head["status"] != abyssal.STATUS_OK:
        return out
    known_mutator = conn.execute(
        "SELECT 1 FROM sde_mutator_ranges WHERE mutator_type_id = ? LIMIT 1",
        (head["mutator_type_id"],),
    ).fetchone() is not None
    rolled = _ROLLED_BY_MUTATOR if known_mutator else _ROLLED_BY_DIFFERENCE
    rows = conn.execute(
        _ABYSSAL_ROLL_ROWS.format(rolled=rolled),
        {"item": item_id, "source": head["source_type_id"], "mutator": head["mutator_type_id"]},
    )
    out["rolls"] = [_roll_dict(r) for r in rows]
    return out


# Text for a fetched item whose mutaplasmid the SDE has never heard of, for
# one whose mutaplasmid is known but whose range still cannot be ranked (the
# source type has no dogma rows, or ESI returned no attributes), and for one
# ESI does not know. The first two are kept apart because they call for
# different remedies: an SDE update fixes the first, nothing fixes the second.
ABYSSAL_SUMMARY_UNRANKED = "Stats fetched — mutator unknown to the SDE"
ABYSSAL_SUMMARY_UNRANKABLE = "Stats fetched — range not rankable"
ABYSSAL_SUMMARY_MISSING = "ESI has no record of this item"


def _abyssal_roll_rows(conn: sqlite3.Connection, item_ids: list[int]):
    """Yield (heads, rolls) per chunk of ids: the row stream abyssal_roll_data reads.

    heads is one row per abyssal_items row in the chunk (item_id, status,
    known -- does the SDE hold any range row for its mutator); rolls is one
    row per ROLLED attribute of every 'ok' item, with the raw inputs to
    position and polarity, the display value, and the labels. Rolled means
    the mutator's own attribute set, the same rule fetch_abyssal_rolls uses
    when it knows the mutator; the difference fallback is not applied here
    because an unranked roll has no quality to summarise or colour, and the
    inspector still shows those rows. Chunked so a very long visible page
    cannot exceed SQLite's bound-parameter limit (999 on older builds).
    """
    ids = sorted({int(i) for i in item_ids})
    # 900 per chunk: comfortably under SQLite's variable limit, and one
    # chunk rather than two for an estate of a few hundred abyssal items.
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        marks = ",".join("?" * len(chunk))
        heads = list(conn.execute(
            f"""SELECT i.item_id, i.status,
                       EXISTS (SELECT 1 FROM sde_mutator_ranges mr
                               WHERE mr.mutator_type_id = i.mutator_type_id) AS known
                FROM abyssal_items i WHERE i.item_id IN ({marks})""",
            chunk,
        ))
        rolls = list(conn.execute(
            f"""
            SELECT aa.item_id,
                   aa.attribute_id,
                   da.name,
                   COALESCE(da.display_name, da.name)   AS label,
                   aa.value                             AS raw_value,
                   {display_value_sql("aa.value", "da.unit_id")} AS value,
                   COALESCE(td.value, da.default_value) AS raw_base,
                   da.high_is_good                      AS attr_high_is_good,
                   mr.high_is_good                      AS mutator_high_is_good,
                   mr.min_mult,
                   mr.max_mult
            FROM abyssal_items i
            JOIN abyssal_attributes    aa ON aa.item_id = i.item_id
            JOIN sde_mutator_ranges    mr ON mr.mutator_type_id = i.mutator_type_id
                                         AND mr.attribute_id = aa.attribute_id
            JOIN sde_dogma_attributes  da ON da.attribute_id = aa.attribute_id
            LEFT JOIN sde_type_dogma   td ON td.type_id = i.source_type_id
                                         AND td.attribute_id = aa.attribute_id
            WHERE i.status = '{abyssal.STATUS_OK}' AND i.item_id IN ({marks})
            ORDER BY aa.item_id, label COLLATE NOCASE, aa.attribute_id
            """,
            chunk,
        ))
        yield heads, rolls


def _row_quality(r: sqlite3.Row) -> float | None:
    high = abyssal.resolve_polarity(
        r["attribute_id"], r["attr_high_is_good"], r["mutator_high_is_good"]
    )
    position = abyssal.roll_position(r["raw_value"], r["raw_base"], r["min_mult"], r["max_mult"])
    return abyssal.quality(position, high)


def abyssal_roll_data(
    conn: sqlite3.Connection, item_ids: list[int]
) -> tuple[dict[int, str], dict[int, dict[int, tuple[float, float | None]]]]:
    """The badge tooltip line and the roll cells for a page of items, in one pass.

    summaries is one line per item with an abyssal_items row: "Speed 75% ·
    Range 88%" for a ranked item, or a fixed explanatory line for a 404, a
    mutator the SDE has never heard of, and a known mutator that still
    yields no rankable roll -- so the tooltip never goes silent on a fetched
    item. A missing key is the model's cue to say "rolls not fetched". The
    known/unknown split is decided the way fetch_abyssal_rolls decides it,
    does the SDE hold any range row for the mutator, rather than inferred
    from whether a quality came out, which once labelled an item with a
    perfectly well-known mutaplasmid "unknown to the SDE" because its
    source type had no dogma rows.

    cells is {item_id: {attribute_id: (display value, quality 0..1 or
    None)}} for the columns the table grows with one type selected: the
    mutator's rolled attributes only, of 'ok' items only; a fetched item
    whose rolls cannot be ranked keeps its cells with quality None, so the
    column shows the value with no tint rather than a blank. Values are
    display numbers (display_value_sql), the same the inspector and the
    `stat:` filter use, and the quality is the same abyssal.quality the
    summaries round.

    One query for the whole visible page rather than one per row, and both
    halves from one row stream: the table reload needs both for the same
    items, and reading the stream once is half the database work.
    """
    summaries: dict[int, str] = {}
    cells: dict[int, dict[int, tuple[float, float | None]]] = {}
    parts: dict[int, list[str]] = {}
    for heads, rolls in _abyssal_roll_rows(conn, item_ids):
        for r in heads:
            if r["status"] != abyssal.STATUS_OK:
                text = ABYSSAL_SUMMARY_MISSING
            elif r["known"]:
                text = ABYSSAL_SUMMARY_UNRANKABLE
            else:
                text = ABYSSAL_SUMMARY_UNRANKED
            summaries[int(r["item_id"])] = text
        for r in rolls:
            item_id = int(r["item_id"])
            q = _row_quality(r)
            cells.setdefault(item_id, {})[int(r["attribute_id"])] = (r["value"], q)
            if q is None:
                continue
            label = abyssal.short_label(r["name"], r["label"])
            parts.setdefault(item_id, []).append(f"{label} {round(q * 100)}%")
    for item_id, bits in parts.items():
        summaries[item_id] = " · ".join(bits)
    return summaries, cells


def abyssal_type_counts(
    conn: sqlite3.Connection, where: str = "", params: tuple = ()
) -> list[sqlite3.Row]:
    """Owned abyssal types with item and fetched counts, faceted by a filter.

    Rows of (type_id, name, items, fetched), busiest type first, then by
    name. The WHERE is written against ASSET_ROWS' inner aliases and
    injected inside the subquery like group_names does. The caller drops
    the abyssal, stat: and roll: chips (both polarities) first, so the
    picker facets by every filter it is not about to rewrite and still
    lists the types the current chips exclude -- the same reason the rail
    excludes its own level. items counts asset rows (abyssal items are
    singletons, so that is the item count); fetched counts rows with status
    'ok'. 404'd items are neither, so items - fetched is not the pending
    count; see abyssal_pending_count.
    """
    sql = f"""
        SELECT r.type_id,
               r.item                                              AS name,
               COUNT(*)                                            AS items,
               SUM(CASE WHEN i.status = '{abyssal.STATUS_OK}' THEN 1 ELSE 0 END) AS fetched
        FROM ({ASSET_ROWS} {f"WHERE {where}" if where else ""}) r
        LEFT JOIN abyssal_items i ON i.item_id = r.item_id
        WHERE r.is_dynamic_type = 1
        GROUP BY r.type_id, r.item
        ORDER BY items DESC, name COLLATE NOCASE
    """
    return list(conn.execute(sql, params))


def abyssal_type_attributes(conn: sqlite3.Connection, type_name: str) -> list[dict]:
    """The attributes any mutaplasmid rolls on one abyssal type, for the card's pickers.

    One dict per attribute (attribute_id, name, label, unit_id, unit,
    high_is_good), ordered by label. Resolved through
    sde_mutator_ranges.resulting_type_id rather than through the estate's
    fetched items, so the picker offers the full set even before any item
    of the type has been fetched. Several mutaplasmids (Decayed, Gravid,
    Unstable, ...) produce the same type with the same attribute set, hence
    the grouping by attribute.

    high_is_good is the STORED polarity, resolved as abyssal.resolve_polarity
    does for a roll: the mutaplasmid's own override first, then the
    attribute's flag. The override is CCP's per-mutator sign fix and every
    mutaplasmid of one type carries the same one (a webifier's speedFactor
    is low-is-good on Decayed, Gravid and Unstable alike), so the MAX over
    the type's mutators is the one value they share; MAX also skips NULLs,
    so one mutator with an override and one without still yields it. The
    card lays display values out worst to best, so it wants this flag
    translated through abyssal.display_high_is_good first.

    Display names are not unique: 554 signatureRadiusBonus (unit 124, a
    percent) and 983 signatureRadiusAdd (unit 1, metres) both display
    "Signature Radius Modifier" (build 3487903). The picker keys on
    attribute_id, and a shared label is disambiguated with the unit symbol
    in brackets -- "Signature Radius Modifier (%)" against "(m)" -- falling
    back to the internal name when a unit is missing or itself shared.
    """
    rows = conn.execute(
        """SELECT da.attribute_id, da.name,
                  COALESCE(da.display_name, da.name) AS label,
                  da.unit_id, du.display_name AS unit,
                  da.high_is_good AS attr_high_is_good,
                  MAX(mr.high_is_good) AS mutator_high_is_good
           FROM sde_mutator_ranges mr
           JOIN sde_types t ON t.type_id = mr.resulting_type_id
           JOIN sde_dogma_attributes da ON da.attribute_id = mr.attribute_id
           LEFT JOIN sde_dogma_units du ON du.unit_id = da.unit_id
           WHERE t.name = ?
           GROUP BY da.attribute_id, da.name, label, da.unit_id, du.display_name,
                    da.high_is_good""",
        (type_name,),
    ).fetchall()
    attrs = []
    for r in rows:
        a = dict(r)
        a["high_is_good"] = abyssal.resolve_polarity(
            a["attribute_id"], a.pop("attr_high_is_good"), a.pop("mutator_high_is_good")
        )
        attrs.append(a)
    by_label: dict[str, list[dict]] = {}
    for a in attrs:
        by_label.setdefault(a["label"], []).append(a)
    for group in by_label.values():
        if len(group) < 2:
            continue
        units = [a["unit"] for a in group]
        use_units = all(units) and len(set(units)) == len(units)
        for a in group:
            a["label"] = f"{a['label']} ({a['unit'] if use_units else a['name']})"
    attrs.sort(key=lambda a: (a["label"].casefold(), a["attribute_id"]))
    return attrs


def abyssal_attribute_bounds(
    conn: sqlite3.Connection, type_name: str
) -> dict[int, tuple[float, float]]:
    """Estate MIN and MAX of each rolled attribute's DISPLAY value for one type.

    The card's sliders are scoped to what the user actually owns of that
    type -- CPU alone can span tens of tf between two module types, so a
    single global range would leave every slider crammed into a corner.
    The extremes are taken AFTER display_value_sql, because units 108 and
    111 display (1 - v) * 100 and so invert the order: the raw minimum is
    the displayed maximum. Rolled attributes of fetched ('ok') items only;
    a type with nothing fetched has no bounds and the card falls back to
    its own defaults.
    """
    dv = display_value_sql("sa.value", "sd.unit_id")
    rows = conn.execute(
        f"""SELECT sa.attribute_id, MIN({dv}) AS lo, MAX({dv}) AS hi
            FROM assets a
            JOIN sde_types t ON t.type_id = a.type_id
            JOIN abyssal_items i ON i.item_id = a.item_id AND i.status = '{abyssal.STATUS_OK}'
            JOIN abyssal_attributes sa ON sa.item_id = i.item_id
            JOIN sde_mutator_ranges mr ON mr.mutator_type_id = i.mutator_type_id
                                      AND mr.attribute_id = sa.attribute_id
            JOIN sde_dogma_attributes sd ON sd.attribute_id = sa.attribute_id
            WHERE t.name = ?
            GROUP BY sa.attribute_id""",
        (type_name,),
    )
    return {int(r["attribute_id"]): (float(r["lo"]), float(r["hi"])) for r in rows}


def abyssal_attribute_bases(conn: sqlite3.Connection, type_name: str) -> dict[int, float]:
    """The un-mutated DISPLAY value of each rollable attribute for one type.

    One abyssal type is made from several source modules -- an Abyssal
    Stasis Webifier from a Webifier I, a II, a Fleeting or a Federation Navy
    -- and each source has its own base, so a type has no single base the
    way an item does. The card's track still wants one tick per attribute,
    and the figure chosen is the base of the type's DOMINANT source: the
    source module the most fetched items of the type were made from (ties
    broken by the lower type id, so the answer is stable between calls). It
    is the figure the inspector shows for most of the items the track spans,
    which a median of per-item bases would not be for any of them. A type
    with no fetched item has no dominant source and no bases.

    Per attribute the base is the source's dogma value, falling back to the
    attribute's SDE default when the source has no row for it -- the same
    COALESCE the inspector's roll rows use -- put through display_value_sql
    so it is comparable with abyssal_attribute_bounds' extremes.
    """
    dv = display_value_sql("COALESCE(td.value, da.default_value)", "da.unit_id")
    rows = conn.execute(
        f"""WITH dominant AS (
                SELECT i.source_type_id AS type_id
                FROM assets a
                JOIN sde_types t ON t.type_id = a.type_id
                JOIN abyssal_items i ON i.item_id = a.item_id
                                    AND i.status = '{abyssal.STATUS_OK}'
                WHERE t.name = ? AND i.source_type_id IS NOT NULL
                GROUP BY i.source_type_id
                ORDER BY COUNT(*) DESC, i.source_type_id
                LIMIT 1
            )
            SELECT DISTINCT mr.attribute_id, {dv} AS base
            FROM sde_mutator_ranges mr
            JOIN sde_types t ON t.type_id = mr.resulting_type_id
            JOIN sde_dogma_attributes da ON da.attribute_id = mr.attribute_id
            JOIN dominant d
            LEFT JOIN sde_type_dogma td ON td.type_id = d.type_id
                                       AND td.attribute_id = mr.attribute_id
            WHERE t.name = ?""",
        (type_name, type_name),
    )
    return {
        int(r["attribute_id"]): float(r["base"]) for r in rows if r["base"] is not None
    }


def abyssal_type_columns(
    conn: sqlite3.Connection, type_name: str
) -> tuple[list[dict], dict[int, tuple[float, float]]]:
    """The rolled attributes of one type that the estate holds values for, with their bounds.

    abyssal_type_attributes lists what the type's mutaplasmids CAN roll, and
    that list is wider than what any owned item carries: the range table
    names attributes the source module lacks (a bonus the base type has at
    zero is "rolled" to zero and never reported by ESI), so a picker or a
    table built from it offers phantom stats -- a column blank on every row,
    a slider with no range to run between. The attribute list is therefore
    cut down to the keys of abyssal_attribute_bounds, which come from the
    fetched items themselves, and returned with those bounds so the two can
    never disagree about which attributes exist. Label order and the
    disambiguated labels are abyssal_type_attributes' own; each attribute
    also carries the type's base display value under "base"
    (abyssal_attribute_bases), or None when no fetched item can name a
    source, so the card can tick the un-mutated figure on its tracks.

    A type with nothing fetched yet has no bounds and so no attributes: the
    card offers no stat rows and the table grows no columns for it until a
    fetch (the card's banner) gives them something to show.
    """
    bounds = abyssal_attribute_bounds(conn, type_name)
    bases = abyssal_attribute_bases(conn, type_name)
    attrs = [
        a for a in abyssal_type_attributes(conn, type_name) if int(a["attribute_id"]) in bounds
    ]
    for a in attrs:
        a["base"] = bases.get(int(a["attribute_id"]))
    return attrs, bounds


def abyssal_pending_count(conn: sqlite3.Connection, type_names: list[str] | None = None) -> int:
    """How many owned abyssal items still have no rolls stored, for the card's banner.

    Wraps abyssal.pending, so it counts exactly what the fetch job would
    ask ESI about: dynamic-type assets with no abyssal_items row, 404'd
    items excluded. type_names narrows it to the chip's types; None or an
    empty list means every type, mirroring the chip, whose empty value is
    "all abyssal types" -- so `abyssal_pending_count(conn,
    omni.split_types(chip.value))` reads the chip correctly either way.
    """
    todo = abyssal.pending(conn)
    if not type_names:
        return len(todo)
    marks = ",".join("?" * len(type_names))
    wanted = {
        int(r[0]) for r in conn.execute(
            f"SELECT type_id FROM sde_types WHERE name IN ({marks})", list(type_names)
        )
    }
    return sum(1 for _item_id, type_id in todo if type_id in wanted)


# ---------------------------------------------------------------- structures
STRUCTURES_SQL = """
SELECT    s.structure_id,
          COALESCE(s.name, 'Structure ' || s.structure_id) AS name,
          ty.name        AS type_name,
          sys.name       AS system_name,
          reg.name       AS region_name,
          sys.security   AS security,
          c.name         AS owner_name,
          s.state,
          s.state_timer_start,
          s.state_timer_end,
          s.fuel_expires,
          s.reinforce_hour,
          s.next_reinforce_hour,
          s.next_reinforce_apply,
          s.unanchors_at,
          s.services,
          s.updated_at,
          s.gone_at,
          x.moon_id,
          x.chunk_arrival_time,
          x.natural_decay_time,
          x.extraction_start_time
FROM      structures s
LEFT JOIN sde_types        ty  ON ty.type_id    = s.type_id
LEFT JOIN sde_systems      sys ON sys.system_id = s.system_id
LEFT JOIN sde_regions      reg ON reg.region_id = s.region_id
LEFT JOIN corporations     co  ON co.corporation_id = s.owner_id
LEFT JOIN characters       c   ON c.corporation_id  = s.owner_id
LEFT JOIN moon_extractions x   ON x.structure_id    = s.structure_id
WHERE     s.owned = 1
GROUP BY  s.structure_id
"""


def fetch_structures(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Structures our own corporations own, with any moon drill cycle attached.

    Deliberately restricted to owned=1. The same table also holds structures
    seen only as a location -- somebody else's Astrahus that one of our ships
    is sitting in -- and those have no fuel clock, no state and no timer, so
    listing them would be pages of blank rows.
    """
    return list(conn.execute(STRUCTURES_SQL))


def structure_owners(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT c.name AS name
           FROM structures s
           JOIN characters c ON c.corporation_id = s.owner_id
           WHERE s.owned = 1 AND c.name IS NOT NULL
           ORDER BY c.name COLLATE NOCASE"""
    )
    return [r["name"] for r in rows]
