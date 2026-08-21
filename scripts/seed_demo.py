#!/usr/bin/env python3
"""Fill a throwaway database with plausible data, no EVE account needed.

Useful for working on the UI, taking screenshots, and checking a change did
not break a view you were not thinking about.

    EVASSET_DATA_DIR=/tmp/demo uv run python scripts/seed_demo.py
    EVASSET_DATA_DIR=/tmp/demo uv run evasset

The SDE is downloaded on first run and cached, so this is only slow once.
Real type ids and station ids throughout, so the joins exercise real data.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evasset import db, networth, sde  # noqa: E402
from evasset.config import (
    DB_PATH,  # noqa: E402
    Settings,  # noqa: E402
)

CHARON, DOMINIX, TRITANIUM, PLEX = 20185, 645, 34, 44992
JITA_4_4, AMARR_VIII = 60003760, 60008494
JITA_SYS, AMARR_SYS = 30000142, 30002187
THE_FORGE, DOMAIN = 10000002, 10000043

PILOT, ALT, CORP = 91000001, 91000002, 98000001


def seed(conn) -> None:
    conn.executescript("""
        DELETE FROM characters;   DELETE FROM corporations; DELETE FROM assets;
        DELETE FROM wallets;      DELETE FROM prices;       DELETE FROM market_orders;
        DELETE FROM wallet_journal; DELETE FROM wallet_transactions; DELETE FROM names;
        DELETE FROM networth_snapshots;
    """)
    conn.executescript(f"""
        INSERT INTO characters (character_id, name, corporation_id, corporation_name,
                                scopes, enabled, include_corp, last_sync_at)
        VALUES ({PILOT}, 'Vex Aldaran', {CORP}, 'Test Holdings', 'esi-assets.read_assets.v1',
                1, 1, '2026-08-06T06:00:00+00:00'),
               ({ALT}, 'Alt Trader', {CORP}, 'Test Holdings', 'esi-assets.read_assets.v1',
                1, 0, '2026-08-06T06:00:00+00:00');

        INSERT INTO corporations (corporation_id, name, ticker, via_character_id)
        VALUES ({CORP}, 'Test Holdings', 'TSTH', {PILOT});

        INSERT INTO names (id, name, category, updated_at) VALUES
            (11, 'Kaari Vex',         'character',   '2026-08-01'),
            (12, 'Red Frog Freight',  'corporation', '2026-08-01'),
            (13, 'Caldari Navy',      'corporation', '2026-08-01');

        INSERT INTO assets (owner_type, owner_id, item_id, type_id, quantity, location_id,
                            location_flag, location_type, is_singleton, root_location_id,
                            system_id, region_id) VALUES
            ('character',   {PILOT}, 1, {CHARON},    1,        {JITA_4_4},  'Hangar',  'station', 1, {JITA_4_4},  {JITA_SYS},  {THE_FORGE}),
            ('character',   {PILOT}, 2, {DOMINIX},   3,        {JITA_4_4},  'Hangar',  'station', 1, {JITA_4_4},  {JITA_SYS},  {THE_FORGE}),
            ('character',   {PILOT}, 3, {TRITANIUM}, 25000000, {JITA_4_4},  'Hangar',  'station', 0, {JITA_4_4},  {JITA_SYS},  {THE_FORGE}),
            ('character',   {PILOT}, 6, {TRITANIUM}, 400000,   1,           'Cargo',   'item',    0, {JITA_4_4},  {JITA_SYS},  {THE_FORGE}),
            ('character',   {ALT},   4, {DOMINIX},   1,        {AMARR_VIII},'Hangar',  'station', 1, {AMARR_VIII},{AMARR_SYS}, {DOMAIN}),
            ('character',   {ALT},   7, {PLEX},      500,      {AMARR_VIII},'Hangar',  'station', 0, {AMARR_VIII},{AMARR_SYS}, {DOMAIN}),
            ('corporation', {CORP},  5, {TRITANIUM}, 900000,   {JITA_4_4},  'CorpSAG1','station', 0, {JITA_4_4},  {JITA_SYS},  {THE_FORGE});

        INSERT INTO wallets (owner_type, owner_id, division, balance) VALUES
            ('character',   {PILOT}, 1, 4200000000),
            ('character',   {ALT},   1, 90000000),
            ('corporation', {CORP},  1, 15000000000);

        INSERT INTO market_orders (owner_type, owner_id, order_id, type_id, location_id,
                                   is_buy_order, price, volume_remain, escrow) VALUES
            ('character', {PILOT}, 9001, {DOMINIX},   {JITA_4_4}, 0, 200000000, 2, 0),
            ('character', {PILOT}, 9002, {TRITANIUM}, {JITA_4_4}, 1, 4.5, 2000000, 9000000);

        -- Real Jita spreads, sampled 2026-08-06. The Charon is contract-priced,
        -- so its bid and ask are the same single number.
        INSERT INTO prices (type_id, buy_price, sell_price, source, samples, updated_at) VALUES
            ({CHARON},    1502000000, 1608000000, 'jita',         1, '2026-08-06T00:00:00+00:00'),
            ({DOMINIX},    148200000,  155700000, 'jita',         1, '2026-08-06T00:00:00+00:00'),
            ({TRITANIUM},       3.76,       3.99, 'jita',         1, '2026-08-06T00:00:00+00:00'),
            ({PLEX},         4528000,    4770000, 'jita',         1, '2026-08-06T00:00:00+00:00');
    """)

    base = datetime(2026, 8, 6, tzinfo=timezone.utc)

    journal = [
        ("market_transaction", -466_500_000.0, "Market: bought 3 Dominix", 11),
        ("brokers_fee",          -3_498_750.0, "Brokers fee", 13),
        ("transaction_tax",     -11_662_500.0, "Transaction tax", 13),
        ("bounty_prizes",        42_500_000.0, "Bounty prizes", 13),
        ("contract_price",   -1_680_000_000.0, "Contract price: Charon", 12),
        ("player_donation",     250_000_000.0, "Thanks for the hauling", 11),
        ("market_escrow",        -9_000_000.0, "Market escrow", 13),
    ]
    balance = 4_200_000_000.0
    rows = []
    for i, (ref, amount, desc, party) in enumerate(journal):
        balance += amount
        rows.append((
            "character", PILOT, 1, 5000 + i,
            (base - timedelta(days=i, hours=i)).isoformat(timespec="seconds"),
            ref, amount, balance, desc, None, party, PILOT, None, None, 0.0, None,
        ))
    conn.executemany(
        "INSERT INTO wallet_journal (owner_type,owner_id,division,entry_id,date,ref_type,"
        "amount,balance,description,reason,first_party_id,second_party_id,context_id,"
        "context_id_type,tax,tax_receiver_id) VALUES (" + ",".join("?" * 16) + ")",
        rows,
    )

    trades = [
        (1, TRITANIUM, 25_000_000,          4.02, 1),
        (2, DOMINIX,            3, 155_500_000.0, 1),
        (3, TRITANIUM, 10_000_000,          5.98, 0),
        (4, CHARON,             1, 1_680_000_000.0, 0),
        (5, PLEX,             500,   4_741_000.0, 1),
        (6, DOMINIX,            1, 182_500_000.0, 0),
    ]
    conn.executemany(
        "INSERT INTO wallet_transactions (owner_type,owner_id,division,transaction_id,date,"
        "type_id,quantity,unit_price,is_buy,is_personal,client_id,location_id,journal_ref_id) "
        "VALUES (" + ",".join("?" * 13) + ")",
        [
            (
                "character", PILOT, 1, tid,
                (base - timedelta(days=tid * 2)).isoformat(timespec="seconds"),
                type_id, qty, price, buy, 1, 11, JITA_4_4, None,
            )
            for tid, type_id, qty, price, buy in trades
        ],
    )
    conn.commit()

    # A net worth curve with some shape to it, rather than a flat line.
    for week, (pilot_mult, corp_mult) in enumerate(
        [(1.0, 1.0), (1.08, 1.03), (1.02, 1.09), (1.21, 1.12), (1.34, 1.10), (1.29, 1.18)]
    ):
        conn.execute(
            "UPDATE wallets SET balance = ? WHERE owner_id = ?",
            (4_200_000_000 * pilot_mult, PILOT),
        )
        conn.execute(
            "UPDATE wallets SET balance = ? WHERE owner_id = ?",
            (15_000_000_000 * corp_mult, CORP),
        )
        taken = (base - timedelta(days=(5 - week) * 7)).isoformat(timespec="seconds")
        networth.take_snapshot(conn, taken)
    conn.commit()


def main() -> int:
    conn = db.init()
    if sde.installed_build(conn) is None:
        print("Importing the SDE (one time, ~95 MB download)…")
        sde.ensure_current(conn, Settings(), lambda m, p: print(f"  [{p:3d}%] {m}"))
    seed(conn)
    print(f"Seeded {DB_PATH}")
    print(f"  {'Owner':<20} {'Jita buy':>20} {'Jita sell':>20}")
    for b in networth.compute_all(conn):
        print(f"  {b.owner_name:<20} {b.total_buy:>20,.2f} {b.total_sell:>20,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
