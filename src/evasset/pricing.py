"""Item valuation on two bases.

Every type carries a buy price and a sell price:

  buy   Highest bid at Jita 4-4. What you get dumping the lot into standing
        orders right now. The floor.
  sell  Lowest ask at Jita 4-4. What you get if you list it and wait. The
        ceiling.

Both come from Fuzzwork's precomputed aggregates in the same request.

Capital hulls are the awkward case. Titans, supers, carriers, FAXes, dreads,
lancers, capital industrials, freighters and jump freighters are mostly traded
by contract, and plenty of them have an empty order book. Where Jita has real
orders we use them; where it does not, we scan public item-exchange contracts,
keep the ones that are a single hull at a fixed price, trim the outliers and
average what is left. A contract price is one number rather than a bid and an
ask, so it fills both columns and `source` records that it did.

Anything with neither falls back to the SDE base price, flagged as such.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from . import db
from .config import (
    FUZZWORK_AGGREGATES,
    JITA_4_4_STATION_ID,
    Settings,
    user_agent,
)
from .esi.client import ESIClient, ESIError

Progress = Callable[[str, int], None]

JITA = "jita"
CONTRACT_AVG = "contract_avg"
BASE_PRICE = "base_price"

_CHUNK = 800  # type ids per Fuzzwork request


@dataclass
class Quote:
    buy: float
    sell: float
    source: str
    samples: int = 1

    @property
    def priced(self) -> bool:
        return self.buy > 0 or self.sell > 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def owned_type_ids(conn: sqlite3.Connection) -> list[int]:
    """Every type id we hold anywhere: assets, orders, contracts, jobs, blueprints."""
    sql = """
        SELECT type_id FROM assets
        UNION SELECT type_id FROM market_orders
        UNION SELECT type_id FROM contract_items
        UNION SELECT blueprint_type_id FROM industry_jobs WHERE blueprint_type_id IS NOT NULL
        UNION SELECT product_type_id   FROM industry_jobs WHERE product_type_id IS NOT NULL
        UNION SELECT type_id FROM blueprints
    """
    return [r[0] for r in conn.execute(sql) if r[0]]


def contract_priced_type_ids(conn: sqlite3.Connection, settings: Settings) -> set[int]:
    """Type ids whose SDE group name is on the contract-priced list."""
    names = settings.contract_priced_groups
    if not names:
        return set()
    placeholders = ",".join("?" * len(names))
    rows = conn.execute(
        f"SELECT t.type_id FROM sde_types t JOIN sde_groups g USING(group_id) "
        f"WHERE g.name IN ({placeholders})",
        names,
    )
    return {r[0] for r in rows}


# ------------------------------------------------------------------ market
def fetch_jita(
    type_ids: list[int], settings: Settings, progress: Progress | None = None
) -> dict[int, Quote]:
    """Highest bid and lowest ask at Jita 4-4, in one pass.

    A side with no orders comes back as zero rather than a stale number, so
    callers can tell "nobody is buying this" from "it is cheap".
    """
    out: dict[int, Quote] = {}
    if not type_ids:
        return out
    headers = {"User-Agent": user_agent(settings)}
    with httpx.Client(timeout=60, headers=headers) as client:
        for i in range(0, len(type_ids), _CHUNK):
            chunk = type_ids[i : i + _CHUNK]
            r = client.get(
                FUZZWORK_AGGREGATES,
                params={
                    "station": JITA_4_4_STATION_ID,
                    "types": ",".join(str(t) for t in chunk),
                },
            )
            r.raise_for_status()
            for key, val in r.json().items():
                buy = (val or {}).get("buy") or {}
                sell = (val or {}).get("sell") or {}
                # buy.max is the best bid; sell.min is the best ask.
                bid = float(buy.get("max") or 0) if int(buy.get("orderCount") or 0) else 0.0
                ask = float(sell.get("min") or 0) if int(sell.get("orderCount") or 0) else 0.0
                if bid > 0 or ask > 0:
                    out[int(key)] = Quote(buy=bid, sell=ask, source=JITA)
            if progress:
                progress(
                    "Fetching Jita buy and sell prices",
                    int(min(i + _CHUNK, len(type_ids)) * 100 / len(type_ids)),
                )
    return out


# ---------------------------------------------------------------- contracts
def _reject_outliers(values: list[float], iqr_mult: float) -> list[float]:
    """Public contracts include bait at 1 ISK and fat-finger listings at 100x.
    Trim by IQR before averaging so one troll cannot move the number."""
    if iqr_mult <= 0 or len(values) < 4:
        return values
    s = sorted(values)
    n = len(s)
    q1 = s[n // 4]
    q3 = s[(3 * n) // 4]
    iqr = q3 - q1
    if iqr <= 0:
        return values
    lo, hi = q1 - iqr_mult * iqr, q3 + iqr_mult * iqr
    kept = [v for v in s if lo <= v <= hi]
    return kept or values


def fetch_contract_prices(
    client: ESIClient,
    conn: sqlite3.Connection,
    settings: Settings,
    wanted: set[int],
    progress: Progress | None = None,
) -> dict[int, tuple[float, int]]:
    """Average public-contract price for each wanted type id.

    Returns {type_id: (average_price, sample_count)}.

    Public contract listings do not include their contents, so this needs one
    extra call per candidate contract. Two prefilters keep that bounded; see
    Settings.contract_min_volume for why the volume floor is a constant rather
    than something derived from the SDE.
    """
    samples: dict[int, list[float]] = {}
    if not wanted:
        return {}

    min_volume = settings.contract_min_volume
    min_price = settings.contract_min_price

    regions = settings.contract_scan_regions
    for ri, region_id in enumerate(regions):
        try:
            listings = client.all_pages(f"/contracts/public/{region_id}", allow_404=True)
        except ESIError:
            continue

        candidates = [
            c
            for c in listings
            if c.get("type") == "item_exchange"
            and (c.get("price") or 0) >= min_price
            and (c.get("volume") or 0) >= min_volume
        ]
        total = len(candidates) or 1
        for ci, c in enumerate(candidates):
            if progress and ci % 25 == 0:
                pct = int((ri + ci / total) * 100 / max(len(regions), 1))
                progress(f"Scanning contracts in region {region_id}", min(pct, 99))
            try:
                items = (
                    client.get(f"/contracts/public/items/{c['contract_id']}", allow_404=True) or []
                )
            except ESIError:
                continue
            included = [i for i in items if i.get("is_included", True)]
            if len(included) != 1:
                continue  # a hull plus fittings is not a hull price
            it = included[0]
            if it.get("quantity", 1) != 1 or it.get("is_blueprint_copy"):
                continue
            tid = it.get("type_id")
            if tid in wanted:
                samples.setdefault(tid, []).append(float(c["price"]))

    out: dict[int, tuple[float, int]] = {}
    for tid, values in samples.items():
        kept = _reject_outliers(values, settings.contract_outlier_iqr)
        out[tid] = (sum(kept) / len(kept), len(kept))
    if progress:
        progress("Contract pricing done", 100)
    return out


def needs_contract_price(quote: Quote | None, settings: Settings) -> bool:
    """Should this capital hull be priced off contracts instead of the market?

    Default is market-first: only fall back to contracts where Jita has no
    usable orders. Flip contract_price_beats_market to always prefer the
    contract average, which is closer to what a hull actually changes hands
    for even when a token order exists.
    """
    if settings.contract_price_beats_market:
        return True
    if quote is None:
        return True
    return quote.buy <= 0 or quote.sell <= 0


# -------------------------------------------------------------------- store
def store_prices(conn: sqlite3.Connection, quotes: dict[int, Quote]) -> None:
    ts = _now()
    rows = [(tid, q.buy, q.sell, q.source, q.samples, ts) for tid, q in quotes.items()]
    with db.transaction(conn):
        db.upsert_many(
            conn,
            "prices",
            ["type_id", "buy_price", "sell_price", "source", "samples", "updated_at"],
            rows,
        )


def refresh_prices(
    conn: sqlite3.Connection,
    client: ESIClient | None,
    settings: Settings,
    progress: Progress | None = None,
) -> dict[str, int]:
    """Repricing pass over everything currently owned."""
    all_ids = owned_type_ids(conn)
    if not all_ids:
        return {"total": 0}

    # One market pass over everything, capitals included -- a Charon does have
    # a Jita order book, and when it does that beats guessing from contracts.
    quotes: dict[int, Quote] = fetch_jita(all_ids, settings, progress)

    capitals = contract_priced_type_ids(conn, settings) & set(all_ids)
    need_contracts = {t for t in capitals if needs_contract_price(quotes.get(t), settings)}

    contract_hits = 0
    if need_contracts and client is not None:
        got = fetch_contract_prices(client, conn, settings, need_contracts, progress)
        for tid, (price, n) in got.items():
            # A contract is one number, not a bid and an ask.
            quotes[tid] = Quote(buy=price, sell=price, source=CONTRACT_AVG, samples=n)
        contract_hits = len(got)

    # Capitals with a one-sided book and no contract sighting: throw the quote
    # away rather than let the mirroring below run on it.
    #
    # Measured on 2026-08-06: 42 of the 60 capital hulls had a one-sided Jita
    # book, and every titan sat on a lowball bid with no asks at all -- an
    # Avatar bid at 1,324,000 ISK against a contract average of ~170 billion.
    # Mirroring that bid across to the sell side would price a titan like a
    # shuttle and quietly wipe most of a supercap pilot's net worth. The SDE
    # base price is a placeholder, but it is the right order of magnitude, and
    # it shows up flagged as base_price so nobody mistakes it for a market.
    for tid in need_contracts:
        q = quotes.get(tid)
        if q is not None and q.source == JITA and not (q.buy > 0 and q.sell > 0):
            del quotes[tid]

    # Last resort so nothing valuable reads as free.
    missing = [t for t in all_ids if t not in quotes or not quotes[t].priced]
    if missing:
        placeholders = ",".join("?" * len(missing))
        rows = conn.execute(
            f"SELECT type_id, base_price FROM sde_types "
            f"WHERE type_id IN ({placeholders}) AND base_price > 0",
            missing,
        )
        for r in rows:
            base = float(r["base_price"])
            quotes[r["type_id"]] = Quote(buy=base, sell=base, source=BASE_PRICE, samples=0)

    # A one-sided market is normal for thin items. Rather than leave a zero
    # that reads as worthless, mirror the side we do have. Capitals were
    # already excluded from this above, where mirroring is actively dangerous.
    for q in quotes.values():
        if q.buy <= 0 and q.sell > 0:
            q.buy = q.sell
        elif q.sell <= 0 and q.buy > 0:
            q.sell = q.buy

    store_prices(conn, quotes)
    return {
        "total": len(quotes),
        "jita": sum(1 for q in quotes.values() if q.source == JITA),
        "contract_avg": contract_hits,
        "base_price": sum(1 for q in quotes.values() if q.source == BASE_PRICE),
        "unpriced": len(all_ids) - len(quotes),
    }
