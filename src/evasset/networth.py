"""Net worth computation and history, on two price bases.

A snapshot is per owner (character or corporation) and splits into buckets so
that a jump in the total can be explained rather than just observed:

  assets     everything in hangars, ships and containers
  wallet     liquid ISK
  orders     sell orders still on the market, at their listed price
  escrow     ISK locked in outstanding buy orders
  contracts  items sitting in contracts we issued that have not completed
  jobs       output value of manufacturing jobs in progress

The three item-valued buckets are recorded twice: once at Jita buy (dump it all
today) and once at Jita sell (list it and wait). The gap between the two totals
is the spread you would pay for being in a hurry. Wallet, sell orders and
escrow are already ISK and do not move with the basis.

The jobs bucket is unrealised by nature -- the items do not exist yet. It is
included because the ISK and materials that went into them are already gone
from the other buckets, so leaving it out makes net worth dip every time you
start a build.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db

CHAR = "character"
CORP = "corporation"

BUY = "buy"
SELL = "sell"
BASES = (BUY, SELL)


@dataclass
class Breakdown:
    owner_type: str
    owner_id: int
    owner_name: str
    assets_buy: float = 0.0
    assets_sell: float = 0.0
    wallet: float = 0.0
    orders: float = 0.0
    escrow: float = 0.0
    contracts_buy: float = 0.0
    contracts_sell: float = 0.0
    jobs_buy: float = 0.0
    jobs_sell: float = 0.0

    @property
    def liquid(self) -> float:
        """The basis-independent part."""
        return self.wallet + self.orders + self.escrow

    @property
    def total_buy(self) -> float:
        return self.assets_buy + self.contracts_buy + self.jobs_buy + self.liquid

    @property
    def total_sell(self) -> float:
        return self.assets_sell + self.contracts_sell + self.jobs_sell + self.liquid

    def total(self, basis: str = SELL) -> float:
        return self.total_buy if basis == BUY else self.total_sell


def _price_column(basis: str) -> str:
    return "p.buy_price" if basis == BUY else "p.sell_price"


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple) -> float:
    row = conn.execute(sql, params).fetchone()
    return float(row[0] or 0.0)


def assets_value(
    conn: sqlite3.Connection, owner_type: str, owner_id: int, basis: str = SELL
) -> float:
    return _scalar(
        conn,
        f"""SELECT SUM(a.quantity * {_price_column(basis)})
            FROM assets a JOIN prices p USING(type_id)
            WHERE a.owner_type=? AND a.owner_id=?""",
        (owner_type, owner_id),
    )


def wallet_value(conn: sqlite3.Connection, owner_type: str, owner_id: int) -> float:
    return _scalar(
        conn,
        "SELECT SUM(balance) FROM wallets WHERE owner_type=? AND owner_id=?",
        (owner_type, owner_id),
    )


def orders_value(conn: sqlite3.Connection, owner_type: str, owner_id: int) -> float:
    """Sell orders valued at what we asked for, not at Jita -- that is the ISK
    actually coming back if they fill."""
    return _scalar(
        conn,
        """SELECT SUM(volume_remain * price) FROM market_orders
           WHERE owner_type=? AND owner_id=? AND is_buy_order=0""",
        (owner_type, owner_id),
    )


def escrow_value(conn: sqlite3.Connection, owner_type: str, owner_id: int) -> float:
    return _scalar(
        conn,
        """SELECT SUM(escrow) FROM market_orders
           WHERE owner_type=? AND owner_id=? AND is_buy_order=1""",
        (owner_type, owner_id),
    )


def contracts_value(
    conn: sqlite3.Connection, owner_type: str, owner_id: int, basis: str = SELL
) -> float:
    """Items we put into contracts that have not resolved yet."""
    return _scalar(
        conn,
        f"""SELECT SUM(ci.quantity * {_price_column(basis)})
            FROM contracts c
            JOIN contract_items ci USING(contract_id)
            JOIN prices p ON p.type_id = ci.type_id
            WHERE c.owner_type=? AND c.owner_id=?
              AND c.status IN ('outstanding','in_progress')
              AND ci.is_included=1
              AND ( (c.owner_type='character'   AND c.issuer_id=c.owner_id)
                 OR (c.owner_type='corporation' AND c.for_corporation=1) )""",
        (owner_type, owner_id),
    )


def jobs_value(
    conn: sqlite3.Connection, owner_type: str, owner_id: int, basis: str = SELL
) -> float:
    """Manufacturing output still cooking. activity_id 1 is manufacturing."""
    return _scalar(
        conn,
        f"""SELECT SUM(j.runs * COALESCE(t.portion_size,1) * {_price_column(basis)})
            FROM industry_jobs j
            JOIN prices p    ON p.type_id = j.product_type_id
            LEFT JOIN sde_types t ON t.type_id = j.product_type_id
            WHERE j.owner_type=? AND j.owner_id=?
              AND j.activity_id=1
              AND j.status='active'""",
        (owner_type, owner_id),
    )


def compute(
    conn: sqlite3.Connection, owner_type: str, owner_id: int, owner_name: str = ""
) -> Breakdown:
    return Breakdown(
        owner_type=owner_type,
        owner_id=owner_id,
        owner_name=owner_name,
        assets_buy=assets_value(conn, owner_type, owner_id, BUY),
        assets_sell=assets_value(conn, owner_type, owner_id, SELL),
        wallet=wallet_value(conn, owner_type, owner_id),
        orders=orders_value(conn, owner_type, owner_id),
        escrow=escrow_value(conn, owner_type, owner_id),
        contracts_buy=contracts_value(conn, owner_type, owner_id, BUY),
        contracts_sell=contracts_value(conn, owner_type, owner_id, SELL),
        jobs_buy=jobs_value(conn, owner_type, owner_id, BUY),
        jobs_sell=jobs_value(conn, owner_type, owner_id, SELL),
    )


def compute_all(conn: sqlite3.Connection) -> list[Breakdown]:
    out = []
    for r in conn.execute(
        "SELECT character_id, name FROM characters WHERE enabled=1 ORDER BY name"
    ):
        out.append(compute(conn, CHAR, r["character_id"], r["name"]))
    for r in conn.execute(
        "SELECT corporation_id, COALESCE(name,'Corp '||corporation_id) name FROM corporations "
        "WHERE via_character_id IS NOT NULL ORDER BY name"
    ):
        out.append(compute(conn, CORP, r["corporation_id"], r["name"]))
    return out


SNAPSHOT_COLUMNS = [
    "taken_at", "owner_type", "owner_id",
    "assets_buy_isk", "assets_sell_isk",
    "wallet_isk", "orders_isk", "escrow_isk",
    "contracts_buy_isk", "contracts_sell_isk",
    "jobs_buy_isk", "jobs_sell_isk",
    "total_buy_isk", "total_sell_isk",
]


def take_snapshot(conn: sqlite3.Connection, taken_at: str | None = None) -> int:
    """Write one row per owner. Returns the number of rows written."""
    ts = taken_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        (
            ts, b.owner_type, b.owner_id,
            b.assets_buy, b.assets_sell,
            b.wallet, b.orders, b.escrow,
            b.contracts_buy, b.contracts_sell,
            b.jobs_buy, b.jobs_sell,
            b.total_buy, b.total_sell,
        )
        for b in compute_all(conn)
    ]
    if not rows:
        return 0
    with db.transaction(conn):
        db.upsert_many(conn, "networth_snapshots", SNAPSHOT_COLUMNS, rows)
    return len(rows)


_SUMMABLE = [c for c in SNAPSHOT_COLUMNS if c.endswith("_isk")]


def history(
    conn: sqlite3.Connection, owner_type: str | None = None, owner_id: int | None = None
) -> list[sqlite3.Row]:
    if owner_type is None:
        sums = ", ".join(f"SUM({c}) {c}" for c in _SUMMABLE)
        return list(
            conn.execute(
                f"SELECT taken_at, {sums} FROM networth_snapshots "
                f"GROUP BY taken_at ORDER BY taken_at"
            )
        )
    return list(
        conn.execute(
            "SELECT * FROM networth_snapshots WHERE owner_type=? AND owner_id=? ORDER BY taken_at",
            (owner_type, owner_id),
        )
    )


def history_per_owner(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every snapshot, kept split by owner and labelled with their name.

    history() either sums across owners or narrows to exactly one, which
    answers "what is everything worth" and "what is this character worth" but
    never "which of them is carrying the account". Same table, just not
    collapsed on the way out.
    """
    return list(
        conn.execute(
            """SELECT s.*,
                      COALESCE(c.name, co.name, s.owner_type||' '||s.owner_id) AS owner_name
               FROM networth_snapshots s
               LEFT JOIN characters   c  ON s.owner_type='character'   AND c.character_id=s.owner_id
               LEFT JOIN corporations co ON s.owner_type='corporation' AND co.corporation_id=s.owner_id
               ORDER BY owner_name COLLATE NOCASE, s.taken_at"""
        )
    )


def owners_with_history(conn: sqlite3.Connection) -> list[tuple[str, int, str]]:
    rows = conn.execute(
        """SELECT DISTINCT s.owner_type, s.owner_id,
                  COALESCE(c.name, co.name, s.owner_type||' '||s.owner_id) AS name
           FROM networth_snapshots s
           LEFT JOIN characters   c  ON s.owner_type='character'   AND c.character_id=s.owner_id
           LEFT JOIN corporations co ON s.owner_type='corporation' AND co.corporation_id=s.owner_id
           ORDER BY name"""
    )
    return [(r["owner_type"], r["owner_id"], r["name"]) for r in rows]


def latest_per_owner(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT s.*, COALESCE(c.name, co.name, s.owner_type||' '||s.owner_id) owner
               FROM networth_snapshots s
               LEFT JOIN characters   c  ON s.owner_type='character'
                                        AND c.character_id=s.owner_id
               LEFT JOIN corporations co ON s.owner_type='corporation'
                                        AND co.corporation_id=s.owner_id
               WHERE s.snapshot_id IN (
                   SELECT MAX(snapshot_id) FROM networth_snapshots
                   GROUP BY owner_type, owner_id)
               ORDER BY s.total_sell_isk DESC"""
        )
    )


def prune_snapshots(conn: sqlite3.Connection, keep_per_day: int = 1) -> int:
    """Collapse a day's snapshots down to the last one, so syncing six times a
    day does not turn the chart into noise."""
    cur = conn.execute(
        """DELETE FROM networth_snapshots WHERE snapshot_id NOT IN (
               SELECT MAX(snapshot_id) FROM networth_snapshots
               GROUP BY owner_type, owner_id, substr(taken_at, 1, 10)
           )"""
    )
    return cur.rowcount
