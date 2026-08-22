"""Stockpiles: what you want on hand, against what you actually have.

A stockpile is a list of target quantities plus a rule for what counts
towards them. The rule here is narrower than jEveAssets' arbitrary filter
tree -- an owner and a location -- because those two answer the question the
feature exists for: "keep 20 Damage Controls in Jita on this character".

What counts as "have" is a judgement rather than a fact, so the sources past
plain assets are opt-in per stockpile:

  assets     always counted
  orders     items sitting on the market as sell orders. Still yours, one
             click from being yours again -- but not if the point of the
             stockpile is what you can undock with right now.
  jobs       manufacturing output still cooking. Counts what will exist, not
             what does. Useful for planning a build, misleading for a
             fleet-ready count.
  contracts  items inside your outstanding contracts.

Location scope resolves through sde_stations and structures, so a station,
its system or its whole region all work as a scope whichever kind of place
the items are sitting in.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

ANY, REGION, SYSTEM, STATION = "any", "region", "system", "station"
SCOPES = (ANY, REGION, SYSTEM, STATION)

# Station IDs and structure IDs both land in the same location columns, and
# neither table alone covers both, so anything resolving a location to its
# system or region has to look in both.
PLACES = """
    SELECT station_id   AS place_id, system_id, region_id FROM sde_stations
    UNION ALL
    SELECT structure_id AS place_id, system_id, region_id FROM structures
"""


@dataclass
class Stockpile:
    stockpile_id: int
    name: str
    owner_type: str | None
    owner_id: int | None
    location_scope: str
    location_id: int | None
    multiplier: float
    include_orders: bool
    include_jobs: bool
    include_contracts: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Stockpile:
        return cls(
            stockpile_id=row["stockpile_id"],
            name=row["name"],
            owner_type=row["owner_type"],
            owner_id=row["owner_id"],
            location_scope=row["location_scope"] or ANY,
            location_id=row["location_id"],
            multiplier=float(row["multiplier"] or 1),
            include_orders=bool(row["include_orders"]),
            include_jobs=bool(row["include_jobs"]),
            include_contracts=bool(row["include_contracts"]),
        )


# ------------------------------------------------------------------- CRUD
def list_all(conn: sqlite3.Connection) -> list[Stockpile]:
    return [
        Stockpile.from_row(r)
        for r in conn.execute("SELECT * FROM stockpiles ORDER BY name COLLATE NOCASE")
    ]


def get(conn: sqlite3.Connection, stockpile_id: int) -> Stockpile | None:
    row = conn.execute(
        "SELECT * FROM stockpiles WHERE stockpile_id=?", (stockpile_id,)
    ).fetchone()
    return None if row is None else Stockpile.from_row(row)


def create(conn: sqlite3.Connection, name: str, **fields) -> int:
    columns = {
        "name": name,
        "owner_type": fields.get("owner_type"),
        "owner_id": fields.get("owner_id"),
        "location_scope": fields.get("location_scope", ANY),
        "location_id": fields.get("location_id"),
        "multiplier": float(fields.get("multiplier", 1) or 1),
        "include_orders": int(bool(fields.get("include_orders"))),
        "include_jobs": int(bool(fields.get("include_jobs"))),
        "include_contracts": int(bool(fields.get("include_contracts"))),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    cur = conn.execute(
        f"INSERT INTO stockpiles({names}) VALUES({marks})", tuple(columns.values())
    )
    return int(cur.lastrowid)


def update(conn: sqlite3.Connection, stockpile_id: int, **fields) -> None:
    allowed = {
        "name", "owner_type", "owner_id", "location_scope", "location_id",
        "multiplier", "include_orders", "include_jobs", "include_contracts",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    assignments = ", ".join(f"{k}=?" for k in sets)
    conn.execute(
        f"UPDATE stockpiles SET {assignments} WHERE stockpile_id=?",
        (*sets.values(), stockpile_id),
    )


def delete(conn: sqlite3.Connection, stockpile_id: int) -> None:
    # The FK declares ON DELETE CASCADE but SQLite only honours that with
    # PRAGMA foreign_keys=ON, which db.connect sets -- clearing the items
    # explicitly costs nothing and does not depend on that staying true.
    conn.execute("DELETE FROM stockpile_items WHERE stockpile_id=?", (stockpile_id,))
    conn.execute("DELETE FROM stockpiles WHERE stockpile_id=?", (stockpile_id,))


def set_item(conn: sqlite3.Connection, stockpile_id: int, type_id: int, target: float) -> None:
    conn.execute(
        """INSERT INTO stockpile_items(stockpile_id, type_id, target) VALUES(?,?,?)
           ON CONFLICT(stockpile_id, type_id) DO UPDATE SET target=excluded.target""",
        (stockpile_id, type_id, float(target)),
    )


def remove_item(conn: sqlite3.Connection, stockpile_id: int, type_id: int) -> None:
    conn.execute(
        "DELETE FROM stockpile_items WHERE stockpile_id=? AND type_id=?",
        (stockpile_id, type_id),
    )


# -------------------------------------------------------------- filtering
def _owner_clause(pile: Stockpile, alias: str) -> tuple[str, list]:
    if pile.owner_type is None or pile.owner_id is None:
        return "", []
    return f" AND {alias}.owner_type=? AND {alias}.owner_id=?", [
        pile.owner_type, pile.owner_id
    ]


def _place_clause(pile: Stockpile, place_col: str) -> tuple[str, list]:
    """Restrict by location, resolving through stations and structures.

    place_col is whichever column on the source table holds a station or
    structure id. Assets are handled separately because they already carry
    system_id and region_id denormalised, so they need no join at all.
    """
    if pile.location_scope == ANY or pile.location_id is None:
        return "", []
    if pile.location_scope == STATION:
        return f" AND {place_col}=?", [pile.location_id]
    column = "system_id" if pile.location_scope == SYSTEM else "region_id"
    return (
        f" AND {place_col} IN (SELECT place_id FROM ({PLACES}) WHERE {column}=?)",
        [pile.location_id],
    )


def _asset_place_clause(pile: Stockpile) -> tuple[str, list]:
    if pile.location_scope == ANY or pile.location_id is None:
        return "", []
    column = {
        STATION: "a.root_location_id",
        SYSTEM: "a.system_id",
        REGION: "a.region_id",
    }[pile.location_scope]
    return f" AND {column}=?", [pile.location_id]


# ------------------------------------------------------------- counting
def have_by_type(conn: sqlite3.Connection, pile: Stockpile) -> dict[int, dict[str, float]]:
    """{type_id: {source: quantity}} for every type in the stockpile."""
    totals: dict[int, dict[str, float]] = {}

    def add(type_id, source, quantity):
        if not quantity:
            return
        totals.setdefault(int(type_id), {})[source] = (
            totals.setdefault(int(type_id), {}).get(source, 0.0) + float(quantity)
        )

    owner_sql, owner_params = _owner_clause(pile, "a")
    place_sql, place_params = _asset_place_clause(pile)
    for r in conn.execute(
        f"""SELECT a.type_id, SUM(a.quantity) q
            FROM assets a
            WHERE a.type_id IN (SELECT type_id FROM stockpile_items WHERE stockpile_id=?)
            {owner_sql}{place_sql}
            GROUP BY a.type_id""",
        (pile.stockpile_id, *owner_params, *place_params),
    ):
        add(r["type_id"], "assets", r["q"])

    if pile.include_orders:
        owner_sql, owner_params = _owner_clause(pile, "o")
        place_sql, place_params = _place_clause(pile, "o.location_id")
        for r in conn.execute(
            f"""SELECT o.type_id, SUM(o.volume_remain) q
                FROM market_orders o
                WHERE o.is_buy_order=0
                  AND o.type_id IN (SELECT type_id FROM stockpile_items WHERE stockpile_id=?)
                  {owner_sql}{place_sql}
                GROUP BY o.type_id""",
            (pile.stockpile_id, *owner_params, *place_params),
        ):
            add(r["type_id"], "orders", r["q"])

    if pile.include_jobs:
        owner_sql, owner_params = _owner_clause(pile, "j")
        place_sql, place_params = _place_clause(pile, "j.output_location_id")
        for r in conn.execute(
            f"""SELECT j.product_type_id type_id,
                       SUM(j.runs * COALESCE(t.portion_size, 1)) q
                FROM industry_jobs j
                LEFT JOIN sde_types t ON t.type_id = j.product_type_id
                WHERE j.activity_id=1 AND j.status='active'
                  AND j.product_type_id IN
                      (SELECT type_id FROM stockpile_items WHERE stockpile_id=?)
                  {owner_sql}{place_sql}
                GROUP BY j.product_type_id""",
            (pile.stockpile_id, *owner_params, *place_params),
        ):
            add(r["type_id"], "jobs", r["q"])

    if pile.include_contracts:
        owner_sql, owner_params = _owner_clause(pile, "c")
        place_sql, place_params = _place_clause(pile, "c.start_location_id")
        for r in conn.execute(
            f"""SELECT ci.type_id, SUM(ci.quantity) q
                FROM contract_items ci
                JOIN contracts c ON c.contract_id = ci.contract_id
                WHERE ci.is_included=1
                  AND ci.type_id IN (SELECT type_id FROM stockpile_items WHERE stockpile_id=?)
                  {owner_sql}{place_sql}
                GROUP BY ci.type_id""",
            (pile.stockpile_id, *owner_params, *place_params),
        ):
            add(r["type_id"], "contracts", r["q"])

    return totals


def rows(conn: sqlite3.Connection, stockpile_id: int) -> list[dict]:
    """One row per stockpile item, with what is held against what is wanted."""
    pile = get(conn, stockpile_id)
    if pile is None:
        return []

    held = have_by_type(conn, pile)
    out = []
    for r in conn.execute(
        """SELECT si.type_id, si.target, t.name AS item, t.volume,
                  COALESCE(p.sell_price, 0) AS sell_price,
                  COALESCE(p.buy_price, 0)  AS buy_price
           FROM stockpile_items si
           LEFT JOIN sde_types t ON t.type_id = si.type_id
           LEFT JOIN prices    p ON p.type_id = si.type_id
           WHERE si.stockpile_id=?
           ORDER BY t.name COLLATE NOCASE""",
        (stockpile_id,),
    ):
        sources = held.get(r["type_id"], {})
        have = sum(sources.values())
        # The multiplier scales the whole stockpile -- "I want three fleets'
        # worth" -- rather than being baked into each target.
        target = float(r["target"] or 0) * pile.multiplier
        shortfall = max(0.0, target - have)
        out.append({
            "type_id": r["type_id"],
            "item": r["item"] or f"Type {r['type_id']}",
            "target": target,
            "have": have,
            "have_assets": sources.get("assets", 0.0),
            "have_orders": sources.get("orders", 0.0),
            "have_jobs": sources.get("jobs", 0.0),
            "have_contracts": sources.get("contracts", 0.0),
            "shortfall": shortfall,
            # A target of zero is "I track this but want none", which is
            # complete by definition rather than a division by zero.
            "percent": 100.0 if target <= 0 else min(100.0, have / target * 100.0),
            "sell_price": float(r["sell_price"] or 0),
            "buy_price": float(r["buy_price"] or 0),
            "shortfall_isk": shortfall * float(r["sell_price"] or 0),
            "shortfall_m3": shortfall * float(r["volume"] or 0),
        })
    return out


def shopping_list(item_rows: list[dict]) -> str:
    """EVE multibuy text for everything short: "Name<tab>quantity" per line.

    Quantities are whole units because you cannot buy 3.5 of something, and
    rounded up because rounding down leaves you short of the number you just
    said you wanted.
    """
    import math

    lines = []
    for row in item_rows:
        if row["shortfall"] <= 0:
            continue
        lines.append(f"{row['item']}\t{math.ceil(row['shortfall'])}")
    return "\n".join(lines)


def totals(item_rows: list[dict]) -> dict:
    wanted = sum(r["target"] for r in item_rows)
    held = sum(min(r["have"], r["target"]) for r in item_rows)
    return {
        "items": len(item_rows),
        "short": sum(1 for r in item_rows if r["shortfall"] > 0),
        "shortfall_isk": sum(r["shortfall_isk"] for r in item_rows),
        "shortfall_m3": sum(r["shortfall_m3"] for r in item_rows),
        # Progress across the whole stockpile, counting each line only up to
        # its target -- otherwise one heavily overstocked item would report
        # the stockpile as complete while half of it is missing.
        "percent": 100.0 if wanted <= 0 else min(100.0, held / wanted * 100.0),
    }


def places_for_scope(conn: sqlite3.Connection, scope: str) -> list[tuple[int, str]]:
    """Candidate locations for a scope, drawn from where stock actually is.

    Offering every station in New Eden would be a five-thousand-entry combo
    box, almost all of it places you have never been. The useful list is the
    places you are already holding things.
    """
    if scope == REGION:
        sql = """SELECT DISTINCT a.region_id AS id, r.name AS name
                 FROM assets a JOIN sde_regions r ON r.region_id = a.region_id
                 WHERE a.region_id IS NOT NULL ORDER BY r.name COLLATE NOCASE"""
    elif scope == SYSTEM:
        sql = """SELECT DISTINCT a.system_id AS id, s.name AS name
                 FROM assets a JOIN sde_systems s ON s.system_id = a.system_id
                 WHERE a.system_id IS NOT NULL ORDER BY s.name COLLATE NOCASE"""
    elif scope == STATION:
        sql = """SELECT DISTINCT a.root_location_id AS id,
                         COALESCE(pl.name, 'Location ' || a.root_location_id) AS name
                  FROM assets a
                  LEFT JOIN (
                      SELECT station_id AS place_id, name FROM sde_stations
                      UNION ALL
                      SELECT structure_id AS place_id, name FROM structures
                  ) pl ON pl.place_id = a.root_location_id
                  WHERE a.root_location_id IS NOT NULL
                  ORDER BY name COLLATE NOCASE"""
    else:
        return []
    return [(r["id"], r["name"]) for r in conn.execute(sql)]


def search_types(conn: sqlite3.Connection, needle: str, limit: int = 200) -> list[tuple[int, str]]:
    """Published types matching a name fragment, for the add-item picker.

    The wildcards a user can type -- % and _ -- are escaped rather than
    passed through, so searching for "Ammatar_" looks for that text and
    not for "Ammatar" followed by any character at all.
    """
    needle = (needle or "").strip()
    if len(needle) < 2:
        return []
    escaped = (
        needle.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    rows = conn.execute(
        """SELECT type_id, name FROM sde_types
           WHERE published=1 AND name LIKE ? ESCAPE '\\'
           ORDER BY LENGTH(name), name COLLATE NOCASE LIMIT ?""",
        ("%" + escaped + "%", limit),
    )
    return [(r["type_id"], r["name"]) for r in rows]
