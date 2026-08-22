"""Stockpile counting: what counts as "have", and where it counts from."""

from __future__ import annotations

import pytest

from evasset import db, stockpile

TRIT, DAMAGE_CONTROL, DOMINIX = 34, 2048, 645
JITA_4_4, AMARR_STATION, PLAYER_KEEPSTAR = 60003760, 60008494, 1035466617946
JITA_SYSTEM, AMARR_SYSTEM = 30000142, 30002187
THE_FORGE, DOMAIN = 10000002, 10000043


@pytest.fixture
def conn(tmp_path):
    c = db.init(tmp_path / "sp.sqlite")
    c.executescript(
        f"""
        INSERT INTO sde_regions VALUES ({THE_FORGE},'The Forge'),({DOMAIN},'Domain');
        INSERT INTO sde_systems VALUES
            ({JITA_SYSTEM},'Jita',20000020,{THE_FORGE},0.9),
            ({AMARR_SYSTEM},'Amarr',20000322,{DOMAIN},1.0);
        INSERT INTO sde_stations VALUES
            ({JITA_4_4},'Jita IV - Moon 4',{JITA_SYSTEM},{THE_FORGE}),
            ({AMARR_STATION},'Amarr VIII',{AMARR_SYSTEM},{DOMAIN});
        INSERT INTO sde_categories VALUES (6,'Ship',1),(7,'Module',1),(4,'Material',1);
        INSERT INTO sde_groups VALUES (27,6,'Battleship',1),(60,7,'Damage Control',1),(18,4,'Mineral',1);
        INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published) VALUES
            ({TRIT},'Tritanium',18,0.01,1,5,1),
            ({DAMAGE_CONTROL},'Damage Control II',60,5,1,500000,1),
            ({DOMINIX},'Dominix',27,454500,1,153900000,1);
        INSERT INTO prices(type_id,buy_price,sell_price,source,updated_at) VALUES
            ({TRIT},4,5,'jita','2026-08-01'),
            ({DAMAGE_CONTROL},400000,500000,'jita','2026-08-01'),
            ({DOMINIX},150000000,160000000,'jita','2026-08-01');
        INSERT INTO characters(character_id,name,enabled) VALUES (1,'Main',1),(2,'Alt',1);
        """
    )
    return c


def asset(conn, type_id, qty, *, owner=1, station=JITA_4_4, system=JITA_SYSTEM, region=THE_FORGE):
    conn.execute(
        """INSERT INTO assets(owner_type,owner_id,item_id,type_id,quantity,location_id,
                              location_flag,location_type,is_singleton,is_blueprint_copy,
                              root_location_id,system_id,region_id)
           VALUES('character',?,?,?,?,?,'Hangar','station',0,0,?,?,?)""",
        (owner, abs(hash((type_id, qty, owner, station))) % 10**12, type_id, qty,
         station, station, system, region),
    )


def a_pile(conn, **fields) -> stockpile.Stockpile:
    pile_id = stockpile.create(conn, fields.pop("name", "Fleet doctrine"), **fields)
    return stockpile.get(conn, pile_id)


# ------------------------------------------------------------------- basics
def test_a_new_stockpile_starts_empty(conn):
    pile = a_pile(conn)
    assert stockpile.rows(conn, pile.stockpile_id) == []


def test_shortfall_is_target_minus_what_is_held(conn):
    pile = a_pile(conn)
    stockpile.set_item(conn, pile.stockpile_id, DAMAGE_CONTROL, 20)
    asset(conn, DAMAGE_CONTROL, 8)

    row = stockpile.rows(conn, pile.stockpile_id)[0]

    assert row["target"] == 20
    assert row["have"] == 8
    assert row["shortfall"] == 12
    assert row["percent"] == pytest.approx(40.0)


def test_being_overstocked_is_not_a_negative_shortfall(conn):
    """Nothing downstream wants to add up negative shopping lists."""
    pile = a_pile(conn)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 100)
    asset(conn, TRIT, 250)

    row = stockpile.rows(conn, pile.stockpile_id)[0]

    assert row["shortfall"] == 0
    assert row["percent"] == 100.0


def test_the_multiplier_scales_every_target(conn):
    """"Three fleets' worth" without editing each line."""
    pile = a_pile(conn, multiplier=3)
    stockpile.set_item(conn, pile.stockpile_id, DAMAGE_CONTROL, 10)
    asset(conn, DAMAGE_CONTROL, 10)

    row = stockpile.rows(conn, pile.stockpile_id)[0]

    assert row["target"] == 30
    assert row["shortfall"] == 20


def test_a_target_of_zero_is_complete_not_a_division_by_zero(conn):
    pile = a_pile(conn)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 0)

    row = stockpile.rows(conn, pile.stockpile_id)[0]

    assert row["percent"] == 100.0
    assert row["shortfall"] == 0


# ------------------------------------------------------------------ sources
def test_assets_are_always_counted(conn):
    pile = a_pile(conn)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 1000)
    asset(conn, TRIT, 400)

    assert stockpile.rows(conn, pile.stockpile_id)[0]["have_assets"] == 400


def test_sell_orders_only_count_when_asked_for(conn):
    """Items on the market are still yours, but not if the stockpile is about
    what you can undock with right now."""
    conn.execute(
        """INSERT INTO market_orders(owner_type,owner_id,order_id,type_id,location_id,
                                     region_id,is_buy_order,price,volume_remain,volume_total,escrow,issued)
           VALUES('character',1,1,?,?,?,0,500000,5,10,0,'2026-08-01')""",
        (DAMAGE_CONTROL, JITA_4_4, THE_FORGE),
    )
    off = a_pile(conn, name="Off")
    on = a_pile(conn, name="On", include_orders=True)
    for pile in (off, on):
        stockpile.set_item(conn, pile.stockpile_id, DAMAGE_CONTROL, 20)

    assert stockpile.rows(conn, off.stockpile_id)[0]["have"] == 0
    assert stockpile.rows(conn, on.stockpile_id)[0]["have"] == 5


def test_buy_orders_never_count_as_stock(conn):
    """A buy order is ISK you have spent, not an item you hold."""
    conn.execute(
        """INSERT INTO market_orders(owner_type,owner_id,order_id,type_id,location_id,
                                     region_id,is_buy_order,price,volume_remain,volume_total,escrow,issued)
           VALUES('character',1,2,?,?,?,1,400000,50,50,20000000,'2026-08-01')""",
        (DAMAGE_CONTROL, JITA_4_4, THE_FORGE),
    )
    pile = a_pile(conn, include_orders=True)
    stockpile.set_item(conn, pile.stockpile_id, DAMAGE_CONTROL, 20)

    assert stockpile.rows(conn, pile.stockpile_id)[0]["have"] == 0


def test_jobs_count_runs_times_portion_size(conn):
    conn.execute(
        """INSERT INTO industry_jobs(owner_type,owner_id,job_id,installer_id,activity_id,
                                     blueprint_type_id,blueprint_location_id,output_location_id,
                                     facility_id,product_type_id,runs,status,start_date,end_date)
           VALUES('character',1,1,1,1,999,?,?,?,?,7,'active','2026-08-01','2026-08-05')""",
        (JITA_4_4, JITA_4_4, JITA_4_4, DAMAGE_CONTROL),
    )
    pile = a_pile(conn, include_jobs=True)
    stockpile.set_item(conn, pile.stockpile_id, DAMAGE_CONTROL, 20)

    assert stockpile.rows(conn, pile.stockpile_id)[0]["have_jobs"] == 7


def test_delivered_jobs_are_not_still_cooking(conn):
    """Their output is already in assets; counting both double-counts it."""
    conn.execute(
        """INSERT INTO industry_jobs(owner_type,owner_id,job_id,installer_id,activity_id,
                                     blueprint_type_id,blueprint_location_id,output_location_id,
                                     facility_id,product_type_id,runs,status,start_date,end_date)
           VALUES('character',1,2,1,1,999,?,?,?,?,7,'delivered','2026-08-01','2026-08-05')""",
        (JITA_4_4, JITA_4_4, JITA_4_4, DAMAGE_CONTROL),
    )
    pile = a_pile(conn, include_jobs=True)
    stockpile.set_item(conn, pile.stockpile_id, DAMAGE_CONTROL, 20)

    assert stockpile.rows(conn, pile.stockpile_id)[0]["have"] == 0


# ----------------------------------------------------------------- scoping
def test_owner_scope_excludes_the_other_character(conn):
    pile = a_pile(conn, owner_type="character", owner_id=1)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 1000)
    asset(conn, TRIT, 100, owner=1)
    asset(conn, TRIT, 900, owner=2)

    assert stockpile.rows(conn, pile.stockpile_id)[0]["have"] == 100


def test_no_owner_scope_counts_everyone(conn):
    pile = a_pile(conn)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 1000)
    asset(conn, TRIT, 100, owner=1)
    asset(conn, TRIT, 900, owner=2)

    assert stockpile.rows(conn, pile.stockpile_id)[0]["have"] == 1000


def test_station_scope_ignores_stock_elsewhere(conn):
    pile = a_pile(conn, location_scope=stockpile.STATION, location_id=JITA_4_4)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 1000)
    asset(conn, TRIT, 600, station=JITA_4_4)
    asset(conn, TRIT, 400, station=AMARR_STATION, system=AMARR_SYSTEM, region=DOMAIN)

    assert stockpile.rows(conn, pile.stockpile_id)[0]["have"] == 600


def test_region_scope_takes_the_whole_region(conn):
    pile = a_pile(conn, location_scope=stockpile.REGION, location_id=THE_FORGE)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 1000)
    asset(conn, TRIT, 600, station=JITA_4_4)
    asset(conn, TRIT, 400, station=AMARR_STATION, system=AMARR_SYSTEM, region=DOMAIN)

    assert stockpile.rows(conn, pile.stockpile_id)[0]["have"] == 600


def test_a_structure_resolves_to_its_system_like_a_station_does(conn):
    """Location scope has to work the same whether the items are in an NPC
    station or a player structure -- both land in the same columns."""
    conn.execute(
        "INSERT INTO structures(structure_id,name,system_id,region_id,accessible,owned)"
        " VALUES(?,'Jita Keepstar',?,?,1,0)",
        (PLAYER_KEEPSTAR, JITA_SYSTEM, THE_FORGE),
    )
    conn.execute(
        """INSERT INTO market_orders(owner_type,owner_id,order_id,type_id,location_id,
                                     region_id,is_buy_order,price,volume_remain,volume_total,escrow,issued)
           VALUES('character',1,3,?,?,?,0,5,250,250,0,'2026-08-01')""",
        (TRIT, PLAYER_KEEPSTAR, THE_FORGE),
    )
    pile = a_pile(
        conn, location_scope=stockpile.SYSTEM, location_id=JITA_SYSTEM, include_orders=True
    )
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 1000)

    assert stockpile.rows(conn, pile.stockpile_id)[0]["have"] == 250


# --------------------------------------------------------- derived output
def test_shopping_list_is_multibuy_text_of_the_gaps_only(conn):
    pile = a_pile(conn)
    stockpile.set_item(conn, pile.stockpile_id, DAMAGE_CONTROL, 20)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 100)
    asset(conn, TRIT, 500)          # already over target, so not on the list

    text = stockpile.shopping_list(stockpile.rows(conn, pile.stockpile_id))

    assert text == "Damage Control II\t20"


def test_shopping_list_rounds_up(conn):
    """Rounding down leaves you short of the number you just asked for."""
    pile = a_pile(conn)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 10.5)

    assert stockpile.shopping_list(stockpile.rows(conn, pile.stockpile_id)) == "Tritanium\t11"


def test_shortfall_is_valued_and_measured(conn):
    pile = a_pile(conn)
    stockpile.set_item(conn, pile.stockpile_id, DAMAGE_CONTROL, 10)
    asset(conn, DAMAGE_CONTROL, 4)

    row = stockpile.rows(conn, pile.stockpile_id)[0]

    assert row["shortfall_isk"] == pytest.approx(6 * 500000)
    assert row["shortfall_m3"] == pytest.approx(6 * 5)


def test_one_overstocked_line_does_not_report_the_pile_complete(conn):
    """Progress counts each line only up to its own target."""
    pile = a_pile(conn)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 100)
    stockpile.set_item(conn, pile.stockpile_id, DAMAGE_CONTROL, 100)
    asset(conn, TRIT, 100_000)      # wildly over
    asset(conn, DAMAGE_CONTROL, 0)

    summary = stockpile.totals(stockpile.rows(conn, pile.stockpile_id))

    assert summary["percent"] == pytest.approx(50.0)
    assert summary["short"] == 1


# ------------------------------------------------------------------- CRUD
def test_deleting_a_stockpile_takes_its_items(conn):
    pile = a_pile(conn)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 10)

    stockpile.delete(conn, pile.stockpile_id)

    assert stockpile.get(conn, pile.stockpile_id) is None
    left = conn.execute(
        "SELECT COUNT(*) c FROM stockpile_items WHERE stockpile_id=?", (pile.stockpile_id,)
    ).fetchone()["c"]
    assert left == 0


def test_setting_the_same_item_twice_updates_rather_than_duplicates(conn):
    pile = a_pile(conn)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 10)
    stockpile.set_item(conn, pile.stockpile_id, TRIT, 25)

    rows = stockpile.rows(conn, pile.stockpile_id)

    assert len(rows) == 1
    assert rows[0]["target"] == 25
