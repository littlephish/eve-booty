"""Read-side SQL for the UI. Kept apart from the sync code on purpose."""

from __future__ import annotations

import sqlite3

from .config import ASSET_SAFETY_LOCATION_ID

# The station/structure/system a root-level item is sitting in, or the fixed
# "Asset Safety" label for anything CCP repackaged after an eviction/pod loss
# (see ASSET_SAFETY_LOCATION_ID -- a fixed constant, not a real place, so it
# never resolves to a station/structure/system name on its own). Shared
# between ASSET_ROWS itself and OVERVIEW_FILTER_EXPR below so the text a user
# clicks in the Overview tab is the exact same expression filtered on when
# they ask to see just that location in Assets -- no risk of the two
# disagreeing on what a location is "called".
LOCATION_EXPR = f"""CASE WHEN a.root_location_id = {ASSET_SAFETY_LOCATION_ID} THEN 'Asset Safety'
         ELSE COALESCE(st.name, sr.name, rsys.name,
                        'Unknown location ' || a.root_location_id)
    END"""

# Every asset row, already joined to names, location and price.
ASSET_ROWS = f"""
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
    a.is_singleton,
    a.is_blueprint_copy,
    a.root_location_id,
    {LOCATION_EXPR}                                                        AS location,
    sys.name                                                               AS system,
    sys.security                                                           AS security,
    reg.name                                                               AS region,
    COALESCE(p.buy_price, 0)                                               AS buy_price,
    COALESCE(p.sell_price, 0)                                              AS sell_price,
    COALESCE(p.source, 'none')                                             AS price_source,
    a.quantity * COALESCE(p.buy_price, 0)                                  AS buy_value,
    a.quantity * COALESCE(p.sell_price, 0)                                 AS sell_value,
    COALESCE(t.volume, 0)                                                  AS unit_volume,
    a.quantity * COALESCE(t.volume, 0)                                     AS volume
FROM assets a
JOIN      sde_types      t    ON t.type_id      = a.type_id
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

# One WHERE-usable expression per grouping level in OverviewView.LEVELS, so
# right-clicking a rolled-up row there ("Jita IV - Moon 4 ...", "Asset
# Safety", a region, an owner, ...) can hand back a clause that picks out
# exactly the assets that rolled into it. Keyed the same as
# OverviewView.LEVELS' second element and location_totals()'s `col` dict.
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


def location_totals(conn: sqlite3.Connection, level: str = "location") -> list[sqlite3.Row]:
    """Roll assets up by location, system, region, owner, category or group."""
    col = {
        "location": "location",
        "system": "system",
        "region": "region",
        "owner": "owner",
        "category": "category",
        "group": "grp",
    }.get(level, "location")
    sql = f"""
        SELECT COALESCE({col}, 'Unknown') AS label,
               COUNT(*)         AS stacks,
               SUM(quantity)    AS units,
               SUM(buy_value)   AS buy_value,
               SUM(sell_value)  AS sell_value,
               SUM(volume)      AS volume
        FROM ({ASSET_ROWS})
        GROUP BY label
        ORDER BY sell_value DESC
    """
    return list(conn.execute(sql))


OVERVIEW_COLUMNS = [
    ("label", "Grouping"),
    ("stacks", "Stacks"),
    ("units", "Units"),
    ("buy_value", "Buy value"),
    ("sell_value", "Sell value"),
    ("volume", "Volume m3"),
]


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
