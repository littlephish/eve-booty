"""Logic tests that need no network and no Qt."""
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.setdefault("EVASSET_DATA_DIR", TMP)
os.environ.setdefault("EVASSET_CACHE_DIR", TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from evasset import db, fitting, networth, pricing, queries
from evasset.config import ASSET_SAFETY_LOCATION_ID, Settings
from evasset.sde import roman


@pytest.fixture()
def conn(tmp_path):
    c = db.init(tmp_path / "t.sqlite")
    c.executescript("""
      INSERT INTO sde_categories VALUES (6,'Ship',1),(4,'Material',1);
      INSERT INTO sde_groups VALUES (513,6,'Freighter',1),(27,6,'Battleship',1),(18,4,'Mineral',1);
      INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published) VALUES
        (20185,'Charon',513,16250000,1,1000000000,1),
        (645,'Dominix',27,454500,1,153900000,1),
        (34,'Tritanium',18,0.01,1,5,1);
      INSERT INTO sde_regions VALUES (10000002,'The Forge');
      INSERT INTO sde_systems VALUES (30000142,'Jita',20000020,10000002,0.9);
      INSERT INTO sde_stations VALUES (60003760,'Jita IV - Moon 4 - Caldari Navy Assembly Plant',30000142,10000002);
      INSERT INTO characters (character_id,name,corporation_id,corporation_name,scopes,enabled)
        VALUES (100,'Test Pilot',2000,'Test Corp','esi-assets.read_assets.v1',1);
      -- a freighter, a battleship, and tritanium, all docked in Jita
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,location_flag,
                          location_type,is_singleton,root_location_id,system_id,region_id) VALUES
        ('character',100,1,20185,1,60003760,'Hangar','station',1,60003760,30000142,10000002),
        ('character',100,2,645,2,60003760,'Hangar','station',1,60003760,30000142,10000002),
        ('character',100,3,34,1000000,60003760,'Hangar','station',0,60003760,30000142,10000002);
      INSERT INTO wallets VALUES ('character',100,1,500000000);
      INSERT INTO market_orders (owner_type,owner_id,order_id,type_id,is_buy_order,price,
                                 volume_remain,escrow) VALUES
        ('character',100,9001,645,0,200000000,3,0),
        ('character',100,9002,34,1,4.5,100000,450000);
      -- type_id, buy_price, sell_price, source, samples, updated_at
      INSERT INTO prices VALUES (20185,2000000000,2000000000,'contract_avg',12,'2026-08-04T00:00:00+00:00'),
                                (645,   170000000, 180000000,'jita',        1, '2026-08-04T00:00:00+00:00'),
                                (34,          5.0,       5.5,'jita',        1, '2026-08-04T00:00:00+00:00');
    """)
    return c


def test_roman_numerals():
    assert [roman(n) for n in (1, 4, 9, 14, 40)] == ["I", "IV", "IX", "XIV", "XL"]


def test_asset_rows_join_and_value(conn):
    rows = queries.fetch_assets(conn)
    by_item = {r["item"]: r for r in rows}
    assert by_item["Charon"]["location"].startswith("Jita IV - Moon 4")
    assert by_item["Charon"]["region"] == "The Forge"
    assert by_item["Charon"]["price_source"] == "contract_avg"
    # A contract price is one number, so both bases carry it.
    assert by_item["Charon"]["buy_value"] == pytest.approx(2_000_000_000)
    assert by_item["Charon"]["sell_value"] == pytest.approx(2_000_000_000)
    assert by_item["Dominix"]["sell_value"] == pytest.approx(360_000_000)
    assert by_item["Dominix"]["buy_value"] == pytest.approx(340_000_000)
    assert by_item["Tritanium"]["sell_value"] == pytest.approx(5_500_000)
    assert by_item["Tritanium"]["buy_value"] == pytest.approx(5_000_000)


def test_search_narrows(conn):
    where, params = queries.search_clause("charon")
    assert len(queries.fetch_assets(conn, where, params)) == 1
    where, params = queries.search_clause("jita")
    assert len(queries.fetch_assets(conn, where, params)) == 3
    where, params = queries.search_clause("nothing here")
    assert queries.fetch_assets(conn, where, params) == []


def test_group_names_every_level_resolves_to_its_own_column(conn):
    """Each key in ROLLUP_LEVELS must map to a real ASSET_ROWS column; a broken
    mapping would either raise or silently serve the fallback column, so
    three levels are pinned to exact content the fixture makes distinct."""
    seen = 0
    for _display, level in queries.ROLLUP_LEVELS:
        names = queries.group_names(conn, level)
        assert names, f"level {level!r} came back empty over seeded data"
        assert all(isinstance(n, str) for n in names)
        seen += 1
    assert seen == 6, "ROLLUP_LEVELS itself must still carry all six grouping levels"
    # Region and group would collide if the mapping fell back to location --
    # and location itself is pinned exactly, because every other level's name
    # set is non-empty too and a mis-mapped "location" would otherwise pass
    # as someone else's plausible-looking strings.
    assert queries.group_names(conn, "region") == ["The Forge"]
    assert queries.group_names(conn, "group") == ["Battleship", "Freighter", "Mineral"]
    assert queries.group_names(conn, "location") == [
        "Jita IV - Moon 4 - Caldari Navy Assembly Plant"
    ]


def test_group_names_facets_on_the_callers_where_clause(conn):
    """Faceting means narrowing with the other active filters -- a search
    for one item must shrink the group list to that item's group, not keep
    offering picks that would land on an empty table."""
    where, params = queries.search_clause("charon")
    assert queries.group_names(conn, "group", where, params) == ["Freighter"]
    # The same clause faceting a different level: still only Charon's rows.
    assert queries.group_names(conn, "category", where, params) == ["Ship"]


def test_group_names_orders_case_insensitively(conn):
    """Byte-order sorting would banish every lowercase-first name below Z,
    so a lowercase group is seeded to prove COLLATE NOCASE is really on."""
    conn.executescript("""
      INSERT INTO sde_groups VALUES (200,4,'ammo',1);
      INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,published)
        VALUES (300,'Test Ammo',200,0.025,1,1);
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton,root_location_id,
                          system_id,region_id) VALUES
        ('character',100,4,300,10,60003760,'Hangar','station',0,60003760,30000142,10000002);
    """)
    assert queries.group_names(conn, "group") == ["ammo", "Battleship", "Freighter", "Mineral"]


def test_group_names_treats_an_unknown_level_as_location(conn):
    """The level key crosses a UI boundary as a plain string; a typo there
    should degrade to the default grouping, not raise or return nothing."""
    fallback = queries.group_names(conn, "no-such-level")
    assert fallback == queries.group_names(conn, "location")
    assert fallback, "the fallback must serve real labels, not agree on empty"


def test_group_names_leaves_out_null_labels(conn):
    """An asset with no resolved system or region (asset safety, mid-sync)
    must not surface as a None entry that would render as a blank,
    unclickable row."""
    conn.execute(
        f"""INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
              location_flag,location_type,is_singleton,root_location_id,system_id,region_id)
            VALUES ('character',100,5,34,10,{ASSET_SAFETY_LOCATION_ID},'AssetSafety',
                    'asset_safety',0,{ASSET_SAFETY_LOCATION_ID},NULL,NULL)"""
    )
    assert queries.group_names(conn, "region") == ["The Forge"]
    assert queries.group_names(conn, "system") == ["Jita"]


def test_networth_breakdown_on_both_bases(conn):
    b = networth.compute(conn, "character", 100, "Test Pilot")
    assert b.assets_sell == pytest.approx(2_365_500_000)
    assert b.assets_buy == pytest.approx(2_345_000_000)
    # Wallet, sell orders and escrow are already ISK, so the basis cannot move them.
    assert b.wallet == pytest.approx(500_000_000)
    assert b.orders == pytest.approx(600_000_000)   # 3 x 200m sell
    assert b.escrow == pytest.approx(450_000)
    assert b.liquid == pytest.approx(1_100_450_000)
    assert b.total_sell == pytest.approx(3_465_950_000)
    assert b.total_buy == pytest.approx(3_445_450_000)
    assert b.total_sell - b.total_buy == pytest.approx(b.assets_sell - b.assets_buy)


def test_basis_argument_selects_the_column(conn):
    assert networth.assets_value(conn, "character", 100, networth.BUY) == pytest.approx(2_345_000_000)
    assert networth.assets_value(conn, "character", 100, networth.SELL) == pytest.approx(2_365_500_000)


def test_snapshot_and_history(conn):
    networth.take_snapshot(conn, "2026-08-01T12:00:00+00:00")
    networth.take_snapshot(conn, "2026-08-02T12:00:00+00:00")
    hist = networth.history(conn, "character", 100)
    assert len(hist) == 2
    assert hist[-1]["total_sell_isk"] == pytest.approx(3_465_950_000)
    assert hist[-1]["total_buy_isk"] == pytest.approx(3_445_450_000)
    assert networth.owners_with_history(conn)[0][2] == "Test Pilot"


def test_prune_keeps_last_per_day(conn):
    networth.take_snapshot(conn, "2026-08-01T09:00:00+00:00")
    networth.take_snapshot(conn, "2026-08-01T21:00:00+00:00")
    networth.take_snapshot(conn, "2026-08-02T09:00:00+00:00")
    networth.prune_snapshots(conn)
    kept = [r["taken_at"] for r in networth.history(conn, "character", 100)]
    assert kept == ["2026-08-01T21:00:00+00:00", "2026-08-02T09:00:00+00:00"]


def test_contract_priced_groups_resolve_by_name(conn):
    s = Settings(contract_priced_groups=["Freighter"])
    assert pricing.contract_priced_type_ids(conn, s) == {20185}


def test_history_across_owners_sums_both_bases(conn):
    networth.take_snapshot(conn, "2026-08-01T12:00:00+00:00")
    row = networth.history(conn)[0]
    assert row["total_sell_isk"] == pytest.approx(3_465_950_000)
    assert row["total_buy_isk"] == pytest.approx(3_445_450_000)


def test_owned_type_ids_covers_every_source(conn):
    assert set(pricing.owned_type_ids(conn)) == {20185, 645, 34}


def test_outlier_rejection_drops_bait_contracts():
    # ten honest freighter prices plus a 1 ISK bait and a fat-finger
    honest = [2.0e9, 2.1e9, 2.05e9, 1.95e9, 2.2e9, 2.15e9, 1.9e9, 2.0e9, 2.1e9, 2.05e9]
    kept = pricing._reject_outliers(honest + [1.0, 5.0e11], 1.5)
    assert 1.0 not in kept and 5.0e11 not in kept
    assert sum(kept) / len(kept) == pytest.approx(2.05e9, rel=0.05)


def test_outlier_rejection_disabled():
    vals = [1.0, 2.0, 3.0, 1000.0]
    assert pricing._reject_outliers(vals, 0) == vals


def test_container_tree_flattens_to_station(conn):
    """A module inside a can inside a ship inside a station must resolve all
    the way up to the station, not stop at the can."""
    from evasset.esi.sync import Syncer

    conn.execute("DELETE FROM assets")
    conn.executescript("""
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton) VALUES
        ('character',100,500,20185,1,60003760,'Hangar','station',1),   -- freighter in station
        ('character',100,501,34,1,500,'CargoBay','item',0),            -- ore in the freighter
        ('character',100,502,645,1,500,'CargoBay','item',1),           -- ship in the freighter
        ('character',100,503,34,5,502,'Cargo','item',0),               -- ore in that ship
        ('character',100,504,34,7,30000142,'AutoFit','solar_system',0);-- floating in space
    """)
    Syncer(conn, None, Settings()).resolve_locations("character", 100, 100)
    got = {
        r["item_id"]: (r["root_location_id"], r["system_id"], r["region_id"])
        for r in conn.execute("SELECT * FROM assets WHERE owner_type='character'")
    }
    assert got[503] == (60003760, 30000142, 10000002)  # three levels deep
    assert got[501] == (60003760, 30000142, 10000002)
    assert got[504] == (30000142, 30000142, 10000002)  # in space, not docked


def test_container_tree_survives_a_cycle(conn):
    """Malformed data must not hang the resolver."""
    from evasset.esi.sync import Syncer

    conn.execute("DELETE FROM assets")
    conn.executescript("""
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_type) VALUES
        ('character',100,600,34,1,601,'item'),
        ('character',100,601,34,1,600,'item');
    """)
    Syncer(conn, None, Settings()).resolve_locations("character", 100, 100)
    rows = list(conn.execute("SELECT root_location_id, system_id FROM assets"))
    assert all(r["system_id"] is None for r in rows)


class _CallRecordingClient:
    """Records every /universe/structures/{id} lookup resolve_locations makes,
    so a test can prove a given id was, or was not, ever looked up."""

    def __init__(self):
        self.requested_structure_ids: list[int] = []

    def get(self, path, **kw):
        self.requested_structure_ids.append(int(path.rsplit("/", 1)[-1]))
        return None  # pretend nothing is resolvable -- irrelevant to what is asked here


def test_asset_safety_is_never_queried_as_a_structure(conn):
    """location_id 2004 is ESI's fixed constant for Asset Safety, confirmed
    against docs.esi.evetech.net/docs/asset_location_id.html -- it is not a
    real structure id, so asking /universe/structures/2004 about it would be
    a guaranteed 404 on every single sync. A genuinely unknown structure id
    is included too, so this also proves resolution was not simply broken."""
    from evasset.esi.sync import Syncer

    conn.execute("DELETE FROM assets")
    conn.executescript(
        f"""
        INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                            location_flag,location_type,is_singleton) VALUES
          ('character',100,700,34,1,{ASSET_SAFETY_LOCATION_ID},'AssetSafety','asset_safety',0),
          ('character',100,701,34,1,1042000000000,'Hangar','item',0);
        """
    )
    client = _CallRecordingClient()
    Syncer(conn, client, Settings()).resolve_locations("character", 100, 100)

    assert ASSET_SAFETY_LOCATION_ID not in client.requested_structure_ids
    assert 1042000000000 in client.requested_structure_ids

    row = conn.execute(
        "SELECT root_location_id, system_id FROM assets WHERE item_id=700"
    ).fetchone()
    assert row["root_location_id"] == ASSET_SAFETY_LOCATION_ID
    assert row["system_id"] is None


def test_asset_safety_gets_a_clear_location_label(conn):
    """Otherwise it falls through to 'Unknown location 2004', which reads
    like a bug and buries the fact that there is recoverable stuff sitting
    there at all."""
    conn.executescript(
        f"""
        INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                            location_flag,location_type,is_singleton,root_location_id,
                            system_id,region_id) VALUES
          ('character',100,60,34,250,{ASSET_SAFETY_LOCATION_ID},'AssetSafety','asset_safety',
           0,{ASSET_SAFETY_LOCATION_ID},NULL,NULL);
        """
    )
    rows = queries.fetch_assets(conn)
    row = next(r for r in rows if r["item_id"] == 60)
    assert row["location"] == "Asset Safety"
    assert row["system"] is None
    assert row["region"] is None


def test_fetch_fit_returns_only_that_ships_own_children(conn):
    conn.executescript(
        """
        INSERT INTO sde_categories VALUES (7,'Module',1),(8,'Charge',1),(18,'Drone',1);
        INSERT INTO sde_groups VALUES (55,7,'Autocannon',1),(83,8,'Projectile Ammo',1),
          (100,18,'Combat Drone',1);
        INSERT INTO sde_types (type_id,name,group_id,portion_size,published) VALUES
          (2873,'125mm Gatling AutoCannon II',55,1,1),
          (206,'EMP S',83,1,1),
          (2456,'Hobgoblin II',100,1,1);
        -- fit to the Dominix (item_id 2, from the shared fixture): a module
        -- with a charge loaded, and a drone in the bay.
        INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                            location_flag,location_type,is_singleton) VALUES
          ('character',100,70,2873,1,2,'HiSlot0','item',1),
          ('character',100,71,206,50,2,'HiSlot0','item',0),
          ('character',100,72,2456,3,2,'DroneBay','item',0),
          ('character',100,73,34,999,60003760,'Hangar','station',0);  -- NOT on the ship
        """
    )
    rows = queries.fetch_fit(conn, 2)
    assert {r["item_id"] for r in rows} == {70, 71, 72}


def test_group_fit_pairs_a_loaded_charge_with_its_module(conn):
    conn.executescript(
        """
        INSERT INTO sde_categories VALUES (7,'Module',1),(8,'Charge',1);
        INSERT INTO sde_groups VALUES (55,7,'Autocannon',1),(83,8,'Projectile Ammo',1);
        INSERT INTO sde_types (type_id,name,group_id,portion_size,published) VALUES
          (2873,'125mm Gatling AutoCannon II',55,1,1),
          (206,'EMP S',83,1,1);
        INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                            location_flag,location_type,is_singleton) VALUES
          ('character',100,70,2873,1,2,'HiSlot0','item',1),
          ('character',100,71,206,50,2,'HiSlot0','item',0);
        """
    )
    rows = queries.fetch_fit(conn, 2)
    groups = dict(fitting.group_fit(rows))
    assert [line.text for line in groups["High slots"]] == [
        "125mm Gatling AutoCannon II  -  loaded: 50 x EMP S"
    ]


def test_group_fit_lists_distinct_slots_in_slot_order(conn):
    conn.executescript(
        """
        INSERT INTO sde_categories VALUES (7,'Module',1);
        INSERT INTO sde_groups VALUES (55,7,'Autocannon',1),(56,7,'Beam Laser',1);
        INSERT INTO sde_types (type_id,name,group_id,portion_size,published) VALUES
          (2873,'125mm Gatling AutoCannon II',55,1,1),
          (3057,'Focused Beam Laser II',56,1,1);
        -- inserted out of slot order, on purpose
        INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                            location_flag,location_type,is_singleton) VALUES
          ('character',100,74,3057,1,2,'HiSlot1','item',1),
          ('character',100,75,2873,1,2,'HiSlot0','item',1);
        """
    )
    rows = queries.fetch_fit(conn, 2)
    groups = dict(fitting.group_fit(rows))
    assert [line.text for line in groups["High slots"]] == [
        "125mm Gatling AutoCannon II",
        "Focused Beam Laser II",
    ]


def test_to_eft_matches_pyfas_own_format(conn):
    """Cross-checked line by line against pyfa-org/Pyfa's service/port/eft.py
    (exportEft/exportModules/exportDrones/exportCargo) -- slot export order
    (low, med, high, not the dialog's high-to-low display order), the
    "Module, Charge" syntax, and the blank-line spacing between sections all
    come from there, not a guess."""
    conn.executescript(
        """
        INSERT INTO sde_categories VALUES (7,'Module',1),(8,'Charge',1),(18,'Drone',1);
        INSERT INTO sde_groups VALUES (55,7,'Autocannon',1),(83,8,'Projectile Ammo',1),
          (100,18,'Combat Drone',1),(60,7,'Damage Control',1);
        INSERT INTO sde_types (type_id,name,group_id,portion_size,published) VALUES
          (2873,'125mm Gatling AutoCannon II',55,1,1),
          (206,'EMP S',83,1,1),
          (2456,'Hobgoblin II',100,1,1),
          (2048,'Damage Control II',60,1,1);
        INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                            location_flag,location_type,is_singleton) VALUES
          ('character',100,90,2048,1,2,'LoSlot0','item',1),
          ('character',100,91,2873,1,2,'HiSlot0','item',1),
          ('character',100,92,206,50,2,'HiSlot0','item',0),
          ('character',100,93,2456,3,2,'DroneBay','item',0),
          ('character',100,94,34,12345,2,'Cargo','item',0);
        """
    )
    rows = queries.fetch_fit(conn, 2)
    text = fitting.to_eft("Dominix", rows)
    expected = (
        "[Dominix, EVE Booty export]\n\n"
        "Damage Control II\n\n"
        "125mm Gatling AutoCannon II, EMP S\n\n\n"
        "Hobgoblin II x3\n\n\n"
        "Tritanium x12345"  # not "x12,345" -- Pyfa's import regex requires plain digits
    )
    assert text == expected


def test_to_eft_leaves_out_holds_eft_has_no_syntax_for(conn):
    """A fleet hangar has no EFT representation -- it must not be emitted as
    something Pyfa would fail to parse."""
    conn.execute(
        "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        "location_flag,location_type,is_singleton) VALUES "
        "('character',100,95,34,10,2,'FleetHangar','item',0)"
    )
    rows = queries.fetch_fit(conn, 2)
    text = fitting.to_eft("Dominix", rows)
    assert text == "[Dominix, EVE Booty export]\n\n"
    assert "FleetHangar" not in text
    assert "Tritanium" not in text


def test_to_esi_fitting_uses_pyfas_inventory_flag_numbers(conn):
    """Flag numbers cross-checked against pyfa-org/Pyfa's INV_FLAGS in
    service/port/esi.py -- low 11, med 19, high 27, rig 92, subsystem 125,
    cargo 5, drone bay 87, fighter bay 158. They are integers on purpose:
    Pyfa's importESI compares flag numerically and sorts the item list by it.
    """
    conn.executescript(
        """
        INSERT INTO sde_categories VALUES (7,'Module',1),(8,'Charge',1),(18,'Drone',1);
        INSERT INTO sde_groups VALUES (55,7,'Autocannon',1),(83,8,'Projectile Ammo',1),
          (100,18,'Combat Drone',1),(60,7,'Damage Control',1);
        INSERT INTO sde_types (type_id,name,group_id,portion_size,published) VALUES
          (2873,'125mm Gatling AutoCannon II',55,1,1),
          (206,'EMP S',83,1,1),
          (2456,'Hobgoblin II',100,1,1),
          (2048,'Damage Control II',60,1,1);
        INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                            location_flag,location_type,is_singleton) VALUES
          ('character',100,90,2048,1,2,'LoSlot0','item',1),
          ('character',100,91,2873,1,2,'HiSlot0','item',1),
          ('character',100,92,206,50,2,'HiSlot0','item',0),
          ('character',100,93,2456,3,2,'DroneBay','item',0),
          ('character',100,94,34,12345,2,'Cargo','item',0);
        """
    )
    rows = queries.fetch_fit(conn, 2)
    fit = fitting.to_esi_fitting("Dominix", 645, rows)

    assert fit["name"] == "Dominix"
    assert fit["ship_type_id"] == 645
    assert fit["description"] == ""
    assert fit["items"] == [
        {"flag": 11, "quantity": 1, "type_id": 2048},   # LoSlot0
        {"flag": 27, "quantity": 1, "type_id": 2873},   # HiSlot0
        {"flag": 5, "quantity": 12345, "type_id": 34},  # cargo
        {"flag": 5, "quantity": 50, "type_id": 206},    # the loaded charge, as cargo
        {"flag": 87, "quantity": 3, "type_id": 2456},   # drone bay
    ]


def test_to_esi_fitting_puts_a_loaded_charge_in_cargo(conn):
    """A charge left on its module's slot flag is silently dropped by Pyfa's
    importESI -- it builds a Module() for any unrecognised flag and swallows
    the ValueError a charge raises. Cargo is the only place it survives."""
    conn.executescript(
        """
        INSERT INTO sde_categories VALUES (7,'Module',1),(8,'Charge',1);
        INSERT INTO sde_groups VALUES (55,7,'Autocannon',1),(83,8,'Projectile Ammo',1);
        INSERT INTO sde_types (type_id,name,group_id,portion_size,published) VALUES
          (2873,'125mm Gatling AutoCannon II',55,1,1),(206,'EMP S',83,1,1);
        INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                            location_flag,location_type,is_singleton) VALUES
          ('character',100,91,2873,1,2,'HiSlot0','item',1),
          ('character',100,92,206,50,2,'HiSlot0','item',0);
        """
    )
    fit = fitting.to_esi_fitting("Rifter", 587, queries.fetch_fit(conn, 2))
    charge = [i for i in fit["items"] if i["type_id"] == 206]
    assert charge == [{"flag": 5, "quantity": 50, "type_id": 206}]


def test_to_esi_fitting_stacks_repeated_types_in_one_entry(conn):
    """Two stacks of the same ammo in the hold are one cargo entry, not two
    -- a fitting lists a type once with a total."""
    conn.executescript(
        """
        INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                            location_flag,location_type,is_singleton) VALUES
          ('character',100,96,34,100,2,'Cargo','item',0),
          ('character',100,97,34,25,2,'Cargo','item',0);
        """
    )
    fit = fitting.to_esi_fitting("Rifter", 587, queries.fetch_fit(conn, 2))
    assert fit["items"] == [{"flag": 5, "quantity": 125, "type_id": 34}]


def test_to_esi_fitting_leaves_out_holds_a_fitting_cannot_express(conn):
    """A fleet hangar has no fitting flag. Emitting it would have importESI
    read the flag as a slot number and invent a module."""
    conn.execute(
        "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        "location_flag,location_type,is_singleton) VALUES "
        "('character',100,95,34,10,2,'FleetHangar','item',0)"
    )
    fit = fitting.to_esi_fitting("Dominix", 645, queries.fetch_fit(conn, 2))
    assert fit["items"] == []


def test_to_esi_fitting_json_is_what_pyfa_sniffs_for(conn):
    """Pyfa's Port.importAuto picks the ESI importer only when the first
    non-blank character of the clipboard is "{"."""
    conn.execute(
        "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        "location_flag,location_type,is_singleton) VALUES "
        "('character',100,98,34,7,2,'Cargo','item',0)"
    )
    fit = fitting.to_esi_fitting("Dominix", 645, queries.fetch_fit(conn, 2))
    text = json.dumps(fit)
    assert text.lstrip()[0] == "{"
    assert json.loads(text)["items"][0]["flag"] == 5


def test_to_esi_fitting_truncates_a_name_past_esis_limit(conn):
    """ESI caps a fitting name at 50 characters."""
    fit = fitting.to_esi_fitting("N" * 80, 645, queries.fetch_fit(conn, 2))
    assert len(fit["name"]) == 50


def test_group_fit_falls_back_for_an_unmapped_flag(conn):
    """A hold type this module has never heard of must still show up under a
    readable label, not vanish."""
    conn.execute(
        "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        "location_flag,location_type,is_singleton) VALUES "
        "('character',100,80,34,3,2,'SomeNewHoldType','item',0)"
    )
    rows = queries.fetch_fit(conn, 2)
    groups = dict(fitting.group_fit(rows))
    assert [line.text for line in groups["Some New Hold Type"]] == ["3 x Tritanium"]


def test_slot_line_carries_the_modules_ids_not_the_loaded_charges(conn):
    """A slot line with a charge loaded is about the module -- its icon and
    rarity tint must come from the module, so the module and charge here get
    deliberately different meta groups to make the assertion mean something."""
    conn.executescript(
        """
        INSERT INTO sde_categories VALUES (7,'Module',1),(8,'Charge',1);
        INSERT INTO sde_groups VALUES (55,7,'Autocannon',1),(83,8,'Projectile Ammo',1);
        INSERT INTO sde_types (type_id,name,group_id,portion_size,published) VALUES
          (2873,'Domination 125mm Autocannon',55,1,1),
          (206,'EMP S',83,1,1);
        UPDATE sde_types SET meta_group_id = 4 WHERE type_id = 2873;  -- Faction module
        UPDATE sde_types SET meta_group_id = 2 WHERE type_id = 206;   -- Tech II charge
        INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                            location_flag,location_type,is_singleton) VALUES
          ('character',100,70,2873,1,2,'HiSlot0','item',1),
          ('character',100,71,206,50,2,'HiSlot0','item',0);
        """
    )
    rows = queries.fetch_fit(conn, 2)
    # fetch_fit itself has to expose meta_group_id, or group_fit has nothing to carry.
    assert {r["type_id"]: r["meta_group_id"] for r in rows} == {2873: 4, 206: 2}
    groups = dict(fitting.group_fit(rows))
    (line,) = groups["High slots"]
    assert line.type_id == 2873
    assert line.meta_group_id == 4, "the tint must follow the module, never the charge"


def test_hold_and_bay_lines_carry_no_ids(conn):
    """Icons and rarity tints are scoped to the slot racks only; that decision
    lives in the FitLine data, so hold/cargo/drone-bay lines must come out
    with both ids unset rather than leaving it to the renderer to re-decide."""
    conn.executescript(
        """
        INSERT INTO sde_categories VALUES (18,'Drone',1);
        INSERT INTO sde_groups VALUES (100,18,'Combat Drone',1);
        INSERT INTO sde_types (type_id,name,group_id,portion_size,published) VALUES
          (2456,'Hobgoblin II',100,1,1);
        UPDATE sde_types SET meta_group_id = 2 WHERE type_id = 2456;
        INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                            location_flag,location_type,is_singleton) VALUES
          ('character',100,72,2456,3,2,'DroneBay','item',0),
          ('character',100,73,34,999,2,'Cargo','item',0);
        """
    )
    rows = queries.fetch_fit(conn, 2)
    seen = 0
    for _label, lines in fitting.group_fit(rows):
        for line in lines:
            seen += 1
            assert line.type_id is None
            assert line.meta_group_id is None
    # Guard against passing vacuously: an empty fit would sail through the
    # loop above without asserting anything at all.
    assert seen >= 2, "expected the drone bay and cargo lines to actually exist"


class _FakeClient:
    """Stands in for ESIClient so the contract scan can be tested offline."""

    def __init__(self, listings, items):
        self.listings = listings
        self.items = items
        self.item_calls = 0

    def all_pages(self, path, **kw):
        return self.listings

    def get(self, path, **kw):
        self.item_calls += 1
        cid = int(path.rsplit("/", 1)[-1])
        return self.items.get(cid, [])


def test_contract_scan_accepts_packaged_capital_volume(conn):
    """Regression: the volume floor must not come from sde_types.volume.

    That column is the assembled volume (Chimera 11,925,000 m3). A contracted
    hull is packaged and reports 1,300,000 m3, so an SDE-derived floor rejects
    every real capital contract. Verified against live data on 2026-08-04.
    """
    conn.execute(
        "INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,published) "
        "VALUES (16233,'Chimera',513,11925000,1,1)"
    )
    listings = [
        # the real one: packaged capital, well under the SDE assembled volume
        {"contract_id": 1, "type": "item_exchange", "price": 2.8e9, "volume": 1_300_000},
        # too cheap to be a hull
        {"contract_id": 2, "type": "item_exchange", "price": 1.0e6, "volume": 1_300_000},
        # a courier, not a sale
        {"contract_id": 3, "type": "courier", "price": 3.0e9, "volume": 1_300_000},
        # a hull bundled with fittings -- not a clean hull price
        {"contract_id": 4, "type": "item_exchange", "price": 9.9e9, "volume": 1_400_000},
        # a stack of small stuff that happens to be expensive
        {"contract_id": 5, "type": "item_exchange", "price": 5.0e9, "volume": 1_000},
    ]
    items = {
        1: [{"type_id": 16233, "quantity": 1, "is_included": True}],
        2: [{"type_id": 16233, "quantity": 1, "is_included": True}],
        4: [
            {"type_id": 16233, "quantity": 1, "is_included": True},
            {"type_id": 645, "quantity": 1, "is_included": True},
        ],
        5: [{"type_id": 34, "quantity": 1000000, "is_included": True}],
    }
    client = _FakeClient(listings, items)
    s = Settings(contract_scan_regions=[10000002], contract_priced_groups=["Freighter"])
    got = pricing.fetch_contract_prices(client, conn, s, {16233}, None)

    assert got == {16233: (2.8e9, 1)}
    # only contracts passing both prefilters should cost an items call
    assert client.item_calls == 2


def test_contract_scan_averages_multiple_sightings(conn):
    conn.execute(
        "INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,published) "
        "VALUES (16233,'Chimera',513,11925000,1,1)"
    )
    prices = [2.6e9, 2.8e9, 3.0e9]
    listings = [
        {"contract_id": i, "type": "item_exchange", "price": p, "volume": 1_300_000}
        for i, p in enumerate(prices, start=1)
    ]
    items = {i: [{"type_id": 16233, "quantity": 1, "is_included": True}] for i in range(1, 4)}
    s = Settings(contract_scan_regions=[10000002])
    got = pricing.fetch_contract_prices(_FakeClient(listings, items), conn, s, {16233}, None)
    assert got[16233][0] == pytest.approx(2.8e9)
    assert got[16233][1] == 3


# ------------------------------------------------------------ wallet history
class _WalletClient:
    """Serves canned journal pages and from_id-cursored transaction batches."""

    def __init__(self, journal=None, tx_batches=None, names=None):
        self.journal = journal or []
        self.tx_batches = tx_batches or {}
        self.names = names or {}
        self.tx_calls = []
        self.name_calls = []

    def all_pages(self, path, **kw):
        return self.journal

    def get(self, path, **kw):
        params = kw.get("params") or {}
        self.tx_calls.append(params.get("from_id"))
        return self.tx_batches.get(params.get("from_id"), [])

    def post(self, path, body, **kw):
        self.name_calls.append(list(body))
        out = [
            {"id": i, "name": self.names[i], "category": "character"}
            for i in body
            if i in self.names
        ]
        return out if len(out) == len(body) else None  # ESI rejects a whole bad batch


def _syncer(conn, client):
    from evasset.esi.sync import Syncer

    return Syncer(conn, client, Settings())


def test_journal_is_append_only(conn):
    """ESI only serves ~30 days. Re-syncing must never drop older rows."""
    old = [{"id": 1, "date": "2026-06-01T00:00:00Z", "ref_type": "player_donation",
            "amount": 100.0, "balance": 100.0, "description": "gift"}]
    _syncer(conn, _WalletClient(journal=old))._char_journal(100)

    newer = [{"id": 2, "date": "2026-07-01T00:00:00Z", "ref_type": "brokers_fee",
              "amount": -5.0, "balance": 95.0, "description": "fee"}]
    _syncer(conn, _WalletClient(journal=newer))._char_journal(100)

    ids = [r[0] for r in conn.execute("SELECT entry_id FROM wallet_journal ORDER BY entry_id")]
    assert ids == [1, 2], "the June entry must survive a sync that no longer returns it"


def test_journal_resync_is_idempotent(conn):
    entries = [{"id": 7, "date": "2026-07-01T00:00:00Z", "ref_type": "bounty_prizes",
                "amount": 1.0, "balance": 1.0, "description": "d"}]
    s = _syncer(conn, _WalletClient(journal=entries))
    assert s._store_journal("character", 100, 1, entries) == 1
    assert s._store_journal("character", 100, 1, entries) == 0


def _tx(tid, date="2026-07-01T00:00:00Z"):
    return {"transaction_id": tid, "date": date, "type_id": 34, "quantity": 1,
            "unit_price": 5.0, "is_buy": False, "is_personal": True,
            "client_id": 9, "location_id": 60003760, "journal_ref_id": tid}


def test_transactions_walk_back_with_from_id(conn):
    """A full batch means there may be more; rewind using the lowest id seen.

    ESI's transactions route has no page parameter. It returns up to 2500 rows
    newest first, and you get the batch before that by passing from_id set to
    the oldest id you have seen.
    """
    from evasset.esi.sync import TX_PAGE_SIZE

    first = [_tx(t) for t in range(3000, 3000 - TX_PAGE_SIZE, -1)]  # exactly full
    oldest = first[-1]["transaction_id"]
    second = [_tx(t) for t in range(oldest - 1, oldest - 101, -1)]  # short -> stop
    client = _WalletClient(tx_batches={None: first, oldest: second})

    got = _syncer(conn, client)._walk_transactions("/x", 100, set())

    assert len(got) == TX_PAGE_SIZE + 100
    assert client.tx_calls == [None, oldest]


def test_transactions_stop_on_already_seen(conn):
    """Routine sync: everything returned is already stored, so one call only."""
    batch = [_tx(t) for t in (10, 9, 8)]
    client = _WalletClient(tx_batches={None: batch})
    s = _syncer(conn, client)
    s._store_transactions("character", 100, 1, batch)
    known = s._known_transaction_ids("character", 100, 1)
    assert known == {8, 9, 10}
    assert s._walk_transactions("/x", 100, known) == []
    assert client.tx_calls == [None]


def test_transaction_query_and_summary(conn):
    rows = [
        {**_tx(1), "is_buy": True, "quantity": 100, "unit_price": 4.0},   # bought 400
        {**_tx(2), "is_buy": False, "quantity": 100, "unit_price": 6.0},  # sold 600
    ]
    _syncer(conn, _WalletClient())._store_transactions("character", 100, 1, rows)
    out = queries.fetch_transactions(conn)
    assert {r["side"] for r in out} == {"Buy", "Sell"}
    assert out[0]["item"] == "Tritanium"
    assert out[0]["location"].startswith("Jita IV - Moon 4")
    s = queries.trade_summary(conn)
    assert (s["bought"], s["sold"], s["net"], s["trades"]) == (400.0, 600.0, 200.0, 2)


def test_name_resolution_splits_a_poisoned_batch(conn):
    """/universe/names rejects the whole call if one id is unresolvable, so a
    failed chunk is retried in halves rather than losing the good ids."""
    client = _WalletClient(names={1: "Alice", 2: "Bob", 4: "Dave"})  # 3 is unresolvable
    _syncer(conn, client).resolve_names({1, 2, 3, 4})
    stored = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM names")}
    assert stored == {1: "Alice", 2: "Bob", 4: "Dave"}
    assert len(client.name_calls) > 1, "should have retried in halves"


def test_name_resolution_skips_ids_already_known(conn):
    conn.execute("INSERT INTO names VALUES (1,'Alice','character','2026-01-01')")
    client = _WalletClient(names={2: "Bob"})
    _syncer(conn, client).resolve_names({1, 2})
    assert client.name_calls == [[2]]


def test_journal_query_resolves_counterparties(conn):
    conn.executescript(
        "INSERT INTO names VALUES (11,'Alice','character','2026-01-01'),"
        "                        (22,'Bob','character','2026-01-01');"
    )
    entries = [{"id": 5, "date": "2026-07-01T00:00:00Z", "ref_type": "player_donation",
                "amount": -1000.0, "balance": 0.0, "description": "d",
                "first_party_id": 11, "second_party_id": 22}]
    _syncer(conn, _WalletClient())._store_journal("character", 100, 1, entries)
    row = queries.fetch_journal(conn)[0]
    assert (row["first_party"], row["second_party"]) == ("Alice", "Bob")
    assert row["owner"] == "Test Pilot"


# ------------------------------------------------------- two-basis pricing
def test_capital_uses_market_when_the_order_book_is_healthy(conn):
    """Default rule: Jita wins for a capital that has both a bid and an ask."""
    s = Settings()
    healthy = pricing.Quote(buy=1.5e9, sell=1.6e9, source=pricing.JITA)
    assert pricing.needs_contract_price(healthy, s) is False


def test_capital_falls_back_to_contracts_when_the_book_is_thin(conn):
    s = Settings()
    assert pricing.needs_contract_price(None, s) is True
    assert pricing.needs_contract_price(pricing.Quote(0, 1.6e9, pricing.JITA), s) is True
    assert pricing.needs_contract_price(pricing.Quote(1.5e9, 0, pricing.JITA), s) is True


def test_contract_first_setting_overrides_a_healthy_book(conn):
    s = Settings(contract_price_beats_market=True)
    healthy = pricing.Quote(buy=1.5e9, sell=1.6e9, source=pricing.JITA)
    assert pricing.needs_contract_price(healthy, s) is True


def test_store_prices_round_trips_both_sides(conn):
    pricing.store_prices(conn, {
        645: pricing.Quote(buy=170e6, sell=180e6, source=pricing.JITA),
        20185: pricing.Quote(buy=2e9, sell=2e9, source=pricing.CONTRACT_AVG, samples=7),
    })
    got = {r["type_id"]: r for r in conn.execute("SELECT * FROM prices")}
    assert (got[645]["buy_price"], got[645]["sell_price"]) == (170e6, 180e6)
    assert got[20185]["source"] == "contract_avg"
    assert got[20185]["samples"] == 7


# -------------------------------------------------------------- migration
def test_migration_upgrades_a_v1_database(tmp_path):
    """A database written before the buy/sell split must keep its history."""
    import sqlite3 as sq

    path = tmp_path / "old.sqlite"
    old = sq.connect(path)
    old.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE prices (
            type_id INTEGER PRIMARY KEY, price REAL NOT NULL, source TEXT NOT NULL,
            samples INTEGER, updated_at TEXT NOT NULL);
        CREATE TABLE networth_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, taken_at TEXT NOT NULL,
            owner_type TEXT NOT NULL, owner_id INTEGER NOT NULL,
            assets_isk REAL DEFAULT 0, wallet_isk REAL DEFAULT 0, orders_isk REAL DEFAULT 0,
            escrow_isk REAL DEFAULT 0, contracts_isk REAL DEFAULT 0, jobs_isk REAL DEFAULT 0,
            total_isk REAL DEFAULT 0);
        INSERT INTO prices VALUES (34, 5.5, 'jita_sell', 1, '2026-08-01T00:00:00+00:00');
        INSERT INTO networth_snapshots
            (taken_at, owner_type, owner_id, assets_isk, wallet_isk, total_isk)
        VALUES ('2026-07-01T00:00:00+00:00', 'character', 100, 1000.0, 500.0, 1500.0);
    """)
    old.commit()
    old.close()

    conn = db.init(path)

    price = conn.execute("SELECT * FROM prices WHERE type_id=34").fetchone()
    assert price["sell_price"] == pytest.approx(5.5), "old price was a sell price"
    assert price["buy_price"] == 0, "buy is unknown until the next repricing run"
    assert price["source"] == "jita"

    snap = conn.execute("SELECT * FROM networth_snapshots").fetchone()
    assert snap["total_sell_isk"] == pytest.approx(1500.0)
    assert snap["assets_sell_isk"] == pytest.approx(1000.0)
    assert snap["wallet_isk"] == pytest.approx(500.0)
    # No invented spread for history we cannot reconstruct.
    assert snap["total_buy_isk"] == 0
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)


def test_migration_leaves_prices_writable(tmp_path):
    """Regression: the very first repricing run after upgrading a v1 database
    crashed with `IntegrityError: NOT NULL constraint failed: prices.price`.

    ALTER TABLE ADD COLUMN cannot loosen the old `price REAL NOT NULL` column
    (no default), so it survived the old migration and blocked every write
    that did not mention it -- which, after the switch to buy_price/sell_price,
    was every write the app makes. Reproduced exactly against a hand-built v1
    table on 2026-08-07, before the migration was changed to rebuild the table
    instead of just adding columns to it."""
    import sqlite3 as sq

    path = tmp_path / "old.sqlite"
    old = sq.connect(path)
    old.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE prices (
            type_id INTEGER PRIMARY KEY, price REAL NOT NULL, source TEXT NOT NULL,
            samples INTEGER, updated_at TEXT NOT NULL);
        INSERT INTO prices VALUES (34, 5.5, 'jita_sell', 1, '2026-08-01T00:00:00+00:00');
    """)
    old.commit()
    old.close()

    conn = db.init(path)
    assert "price" not in db.columns(conn, "prices"), "the legacy column must be gone, not just supplemented"

    # This is the exact call that crashed: a normal repricing run writing a
    # type that was not in the old table at all.
    pricing.store_prices(conn, {645: pricing.Quote(buy=170e6, sell=180e6, source=pricing.JITA)})
    row = conn.execute("SELECT * FROM prices WHERE type_id=645").fetchone()
    assert (row["buy_price"], row["sell_price"]) == (170e6, 180e6)

    # And the migrated row itself must still be there, untouched.
    old_row = conn.execute("SELECT * FROM prices WHERE type_id=34").fetchone()
    assert old_row["sell_price"] == pytest.approx(5.5)


def test_migration_leaves_networth_snapshots_writable(tmp_path):
    """Same failure mode, checked on the other migrated table. Old columns
    here had DEFAULT 0 so this one never actually crashed, but it should be
    held to the same rebuilt-not-patched standard rather than relying on that
    being true by luck."""
    import sqlite3 as sq

    path = tmp_path / "old2.sqlite"
    old = sq.connect(path)
    old.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE networth_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, taken_at TEXT NOT NULL,
            owner_type TEXT NOT NULL, owner_id INTEGER NOT NULL,
            assets_isk REAL DEFAULT 0, wallet_isk REAL DEFAULT 0, orders_isk REAL DEFAULT 0,
            escrow_isk REAL DEFAULT 0, contracts_isk REAL DEFAULT 0, jobs_isk REAL DEFAULT 0,
            total_isk REAL DEFAULT 0);
        INSERT INTO networth_snapshots
            (taken_at, owner_type, owner_id, assets_isk, wallet_isk, total_isk)
        VALUES ('2026-07-01T00:00:00+00:00', 'character', 100, 1000.0, 500.0, 1500.0);
    """)
    old.commit()
    old.close()

    conn = db.init(path)
    assert "assets_isk" not in db.columns(conn, "networth_snapshots")
    n = networth.take_snapshot(conn, "2026-08-07T00:00:00+00:00")
    assert n >= 0  # must not raise; 0 is fine if there are no enabled owners yet


def test_migration_repairs_a_database_stuck_mid_migration(tmp_path):
    """Regression: the fix for the first crash still crashed the same way.

    The first version of this migration used ALTER TABLE ADD COLUMN and got
    as far as adding buy_price/sell_price before the app crashed elsewhere (a
    later, separate store_prices call outside of migrate() itself). That left
    real user databases with `price`, `buy_price` and `sell_price` all present
    at once. The rebuild-based fix that replaced it still checked
    `"buy_price" not in cols` to decide whether to run -- which is false for
    exactly this database, so it silently did nothing and the identical
    IntegrityError kept happening on every subsequent launch. Reproduced
    against a hand-built copy of that exact intermediate state on 2026-08-07."""
    import sqlite3 as sq

    path = tmp_path / "stuck.sqlite"
    old = sq.connect(path)
    old.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE prices (
            type_id INTEGER PRIMARY KEY, price REAL NOT NULL, source TEXT NOT NULL,
            samples INTEGER, updated_at TEXT NOT NULL,
            buy_price REAL NOT NULL DEFAULT 0, sell_price REAL NOT NULL DEFAULT 0);
        INSERT INTO prices VALUES (34, 5.5, 'jita', 1, '2026-08-01T00:00:00+00:00', 0, 5.5);
    """)
    old.commit()
    old.close()

    conn = db.init(path)
    cols = db.columns(conn, "prices")
    assert "price" not in cols, "the legacy column must be dropped even though buy_price already existed"

    pricing.store_prices(conn, {645: pricing.Quote(buy=170e6, sell=180e6, source=pricing.JITA)})
    row = conn.execute("SELECT * FROM prices WHERE type_id=645").fetchone()
    assert (row["buy_price"], row["sell_price"]) == (170e6, 180e6)


def test_migration_repairs_networth_snapshots_stuck_mid_migration(tmp_path):
    """Same defect, same fix, on the other rebuilt table."""
    import sqlite3 as sq

    path = tmp_path / "stuck2.sqlite"
    old = sq.connect(path)
    old.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE networth_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, taken_at TEXT NOT NULL,
            owner_type TEXT NOT NULL, owner_id INTEGER NOT NULL,
            assets_isk REAL DEFAULT 0, wallet_isk REAL DEFAULT 0, orders_isk REAL DEFAULT 0,
            escrow_isk REAL DEFAULT 0, contracts_isk REAL DEFAULT 0, jobs_isk REAL DEFAULT 0,
            total_isk REAL DEFAULT 0,
            assets_buy_isk REAL DEFAULT 0, assets_sell_isk REAL DEFAULT 0,
            contracts_buy_isk REAL DEFAULT 0, contracts_sell_isk REAL DEFAULT 0,
            jobs_buy_isk REAL DEFAULT 0, jobs_sell_isk REAL DEFAULT 0,
            total_buy_isk REAL DEFAULT 0, total_sell_isk REAL DEFAULT 0);
        INSERT INTO networth_snapshots
            (taken_at, owner_type, owner_id, assets_isk, wallet_isk, total_isk,
             assets_sell_isk, total_sell_isk)
        VALUES ('2026-07-01T00:00:00+00:00', 'character', 100, 1000.0, 500.0, 1500.0, 1000.0, 1500.0);
    """)
    old.commit()
    old.close()

    conn = db.init(path)
    cols = db.columns(conn, "networth_snapshots")
    assert "assets_isk" not in cols
    assert networth.take_snapshot(conn, "2026-08-07T00:00:00+00:00") >= 0  # must not raise


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "twice.sqlite"
    db.init(path)
    before = db.columns(db.connect(path), "prices")
    assert db.migrate(db.connect(path)) == []
    assert db.columns(db.connect(path), "prices") == before


class _NoContractsClient:
    """Contract scan that finds nothing, to exercise the fallback path."""

    def all_pages(self, path, **kw):
        return []

    def get(self, path, **kw):
        return []


def test_titan_with_a_lowball_bid_is_not_valued_like_a_shuttle(conn, monkeypatch):
    """Regression, measured against live Jita on 2026-08-06.

    42 of 60 capital hulls had a one-sided order book, and every titan sat on
    a lowball bid with no asks: an Avatar bid at 1,324,000 ISK against a
    contract average near 170 billion. Mirroring the bid across to the sell
    side would price the hull like a shuttle. When there is no contract
    sighting either, the SDE base price has to win.
    """
    conn.execute(
        "INSERT INTO sde_groups VALUES (30,6,'Titan',1)"
    )
    conn.execute(
        "INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published) "
        "VALUES (23773,'Ragnarok',30,155000000,1,60000000000,1)"
    )
    conn.execute(
        "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        "location_type) VALUES ('character',100,42,23773,1,60003760,'station')"
    )

    # Jita: a 1.818m bid, nothing on the ask side.
    monkeypatch.setattr(
        pricing, "fetch_jita",
        lambda ids, s, progress=None: {
            23773: pricing.Quote(buy=1_818_000.0, sell=0.0, source=pricing.JITA)
        },
    )
    settings = Settings(contract_priced_groups=["Titan"], contract_scan_regions=[10000002])
    pricing.refresh_prices(conn, _NoContractsClient(), settings)

    row = conn.execute("SELECT * FROM prices WHERE type_id=23773").fetchone()
    assert row["source"] == "base_price"
    assert row["sell_price"] == pytest.approx(60_000_000_000)
    assert row["buy_price"] == pytest.approx(60_000_000_000)
    assert row["sell_price"] > 1_000_000_000, "a titan must never price like a shuttle"


def test_thin_subcapital_still_mirrors_its_one_sided_book(conn, monkeypatch):
    """The strict rule is for contract-priced groups only. An ordinary item
    with bids but no asks should still be worth its bid, not fall back."""
    monkeypatch.setattr(
        pricing, "fetch_jita",
        lambda ids, s, progress=None: {
            34: pricing.Quote(buy=5.0, sell=0.0, source=pricing.JITA)
        },
    )
    settings = Settings(contract_priced_groups=["Titan"])
    pricing.refresh_prices(conn, _NoContractsClient(), settings)
    row = conn.execute("SELECT * FROM prices WHERE type_id=34").fetchone()
    assert row["source"] == "jita"
    assert (row["buy_price"], row["sell_price"]) == (5.0, 5.0)


def test_capital_with_a_healthy_book_keeps_its_market_price(conn, monkeypatch):
    conn.execute("INSERT INTO sde_groups VALUES (30,6,'Titan',1)")
    conn.execute(
        "INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published) "
        "VALUES (23773,'Ragnarok',30,155000000,1,60000000000,1)"
    )
    conn.execute(
        "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        "location_type) VALUES ('character',100,42,23773,1,60003760,'station')"
    )
    monkeypatch.setattr(
        pricing, "fetch_jita",
        lambda ids, s, progress=None: {
            23773: pricing.Quote(buy=150e9, sell=175e9, source=pricing.JITA)
        },
    )
    settings = Settings(contract_priced_groups=["Titan"])
    pricing.refresh_prices(conn, _NoContractsClient(), settings)
    row = conn.execute("SELECT * FROM prices WHERE type_id=23773").fetchone()
    assert row["source"] == "jita"
    assert (row["buy_price"], row["sell_price"]) == (150e9, 175e9)


# ------------------------------------------------------------ ESI scope list
CONFIRMED_BROKEN_SCOPES = [
    # scope, date confirmed against a live application
    ("esi-corporations.read_blueprints.v1", "2026-08-06"),
    ("esi-corporations.read_divisions.v1", "2026-08-07"),
]


def test_confirmed_broken_scopes_stay_out_of_the_default_request():
    """Both are declared valid by ESI's own spec and by every SDK generated
    from it, but EVE SSO's /v2/oauth/authorize rejects both with
    invalid_scope regardless -- confirmed live, with each scope already
    present and approved on the test application. Because OAuth2 fails the
    entire authorization request over a single bad scope (RFC 6749 4.1.2.1),
    one of these left in SCOPES silently hangs every character's login until
    the app's own timeout. Two for two on corp-specific scopes is a pattern:
    treat this list as something to extend on the next failure, not a closed
    set. Nothing here should come back into SCOPES without someone
    deliberately re-testing it against a live login."""
    from evasset.config import SCOPES

    for scope, _confirmed_on in CONFIRMED_BROKEN_SCOPES:
        assert scope not in SCOPES, f"{scope} was confirmed broken -- keep it out of SCOPES"
    # The character-level blueprints equivalent is unaffected and should stay requested.
    assert "esi-characters.read_blueprints.v1" in SCOPES


# ---------------------------------------------------- rail and strip queries
def test_asset_rows_expose_the_price_quote_age(conn):
    """The stale-price badge needs to know when a quote was taken; a row with
    no price at all must read as NULL, not crash the join."""
    conn.executescript("""
      INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,published)
        VALUES (999,'Paint Widget',18,2,1,1);
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton,root_location_id,
                          system_id,region_id) VALUES
        ('character',100,50,999,3,60003760,'Hangar','station',0,60003760,30000142,10000002);
    """)
    by_item = {r["item"]: r for r in queries.fetch_assets(conn)}
    assert by_item["Charon"]["price_updated_at"] == "2026-08-04T00:00:00+00:00"
    assert by_item["Paint Widget"]["price_updated_at"] is None


def test_rail_rollups_aggregate_and_sort_on_all_three_keys(conn):
    """The fixture is arranged so value, name and volume each produce a
    different label order -- a sort key that silently fell back to another
    would flunk at least one of the three."""
    # A second mineral stack: makes Mineral the top value (2.75b), the middle
    # volume (5m m3), and proves stacks/units really aggregate.
    conn.execute(
        "INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        "location_flag,location_type,is_singleton,root_location_id,system_id,region_id) "
        "VALUES ('character',100,51,34,499000000,60003760,'Hangar','station',0,"
        "60003760,30000142,10000002)"
    )
    by_value = queries.rail_rollups(conn, "group", sort="value")
    assert [r["label"] for r in by_value] == ["Mineral", "Freighter", "Battleship"]
    mineral = by_value[0]
    assert mineral["stacks"] == 2
    assert mineral["units"] == 500_000_000
    assert mineral["volume"] == pytest.approx(5_000_000)
    assert mineral["sell_value"] == pytest.approx(2_750_000_000)

    by_name = queries.rail_rollups(conn, "group", sort="name")
    assert [r["label"] for r in by_name] == ["Battleship", "Freighter", "Mineral"]

    by_volume = queries.rail_rollups(conn, "group", sort="volume")
    assert [r["label"] for r in by_volume] == ["Freighter", "Mineral", "Battleship"]


def test_rail_rollups_facet_on_the_callers_where_clause(conn):
    rows = queries.rail_rollups(conn, "group", "cat.name = ?", ("Ship",))
    assert [r["label"] for r in rows] == ["Freighter", "Battleship"]
    assert rows[0]["sell_value"] == pytest.approx(2_000_000_000)


def test_rail_rollups_drop_null_labels(conn):
    """An asset-safety row has no system; the system-level rail must not
    offer a blank row for it."""
    conn.execute(
        f"INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,"
        f"location_flag,location_type,is_singleton,root_location_id,system_id,region_id) "
        f"VALUES ('character',100,52,34,10,{ASSET_SAFETY_LOCATION_ID},'AssetSafety',"
        f"'asset_safety',0,{ASSET_SAFETY_LOCATION_ID},NULL,NULL)"
    )
    assert [r["label"] for r in queries.rail_rollups(conn, "system")] == ["Jita"]
    # At location level the same row is addressable, under its fixed label.
    labels = [r["label"] for r in queries.rail_rollups(conn, "location")]
    assert "Asset Safety" in labels


def test_where_is_item_sums_quantities_biggest_pile_first(conn):
    rows = queries.where_is_item(conn, "group")
    assert [(r["label"], r["quantity"]) for r in rows] == [
        ("Mineral", 1_000_000), ("Battleship", 2), ("Freighter", 1),
    ]
    faceted = queries.where_is_item(conn, "location", "t.name LIKE ?", ("%Tritanium%",))
    assert [(r["label"], r["quantity"]) for r in faceted] == [
        ("Jita IV - Moon 4 - Caldari Navy Assembly Plant", 1_000_000),
    ]


def test_estate_summary_matches_hand_computed_fixture_values(conn):
    """Assets 2,365.5m at sell, liquid 500m wallet + 600m sell orders + 0.45m
    escrow, volume 16.25m + 0.909m + 0.01m m3 -- every figure below is worked
    out from the fixture by hand, plus one deliberately unpriced stack."""
    conn.executescript("""
      INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,published)
        VALUES (999,'Paint Widget',18,2,1,1);
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton,root_location_id,
                          system_id,region_id) VALUES
        ('character',100,50,999,3,60003760,'Hangar','station',0,60003760,30000142,10000002);
    """)
    s = queries.estate_summary(conn)
    assert s["assets_sell"] == pytest.approx(2_365_500_000)
    assert s["wallet_liquid"] == pytest.approx(1_100_450_000)
    assert s["total"] == pytest.approx(3_465_950_000)
    assert s["volume"] == pytest.approx(17_169_006)  # includes the widget's 3 x 2 m3
    assert s["unpriced_stacks"] == 1


def test_estate_summary_and_value_map_only_count_enabled_owners(conn):
    """The strip is pinned to "all enabled owners", the same roster
    networth.compute_all uses. A disabled character's wealth staying in the
    database (disabling means stop counting, not forget) must not leak into
    any strip figure -- a repro before the fix showed the strip and the Net
    worth tab quoting wildly different liquid ISK over the same file."""
    conn.executescript("""
      INSERT INTO characters (character_id,name,corporation_id,scopes,enabled)
        VALUES (102,'Retired Alt',2000,'s',0);
      INSERT INTO sde_stations VALUES
        (60008494,'Amarr VIII (Oris) - Emperor Family Academy',30002187,10000043);
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton,root_location_id,
                          system_id,region_id) VALUES
        ('character',102,55,20185,1,60008494,'Hangar','station',1,60008494,NULL,NULL);
      INSERT INTO wallets VALUES ('character',102,1,900000000);
      INSERT INTO market_orders (owner_type,owner_id,order_id,type_id,is_buy_order,price,
                                 volume_remain,escrow) VALUES
        ('character',102,9101,645,0,5000000000,10,0),
        ('character',102,9102,34,1,4.5,100000,999000000);
    """)
    s = queries.estate_summary(conn)
    # Identical to the enabled-only fixture numbers: the alt's 2b Charon,
    # 900m wallet, 50b of sell orders and 999m escrow all stay out.
    assert s["assets_sell"] == pytest.approx(2_365_500_000)
    assert s["wallet_liquid"] == pytest.approx(1_100_450_000)
    assert s["total"] == pytest.approx(3_465_950_000)
    assert s["volume"] == pytest.approx(17_169_000)
    assert s["unpriced_stacks"] == 0
    # And the strip agrees with the Net worth tab computed over the same DB.
    assert s["wallet_liquid"] == pytest.approx(
        sum(b.liquid for b in networth.compute_all(conn))
    )
    assert s["assets_sell"] == pytest.approx(
        sum(b.assets_sell for b in networth.compute_all(conn))
    )
    # The value map must not offer the disabled alt's station as a segment.
    assert [r["label"] for r in queries.value_map(conn)] == [
        "Jita IV - Moon 4 - Caldari Navy Assembly Plant"
    ]


def test_value_map_ranks_locations_by_sell_value_and_honours_the_limit(conn):
    conn.executescript("""
      INSERT INTO sde_stations VALUES
        (60008494,'Amarr VIII (Oris) - Emperor Family Academy',30002187,10000043);
      INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
                          location_flag,location_type,is_singleton,root_location_id,
                          system_id,region_id) VALUES
        ('character',100,53,645,1,60008494,'Hangar','station',1,60008494,NULL,NULL);
    """)
    rows = queries.value_map(conn)
    assert [(r["label"], r["sell_value"]) for r in rows] == [
        ("Jita IV - Moon 4 - Caldari Navy Assembly Plant", pytest.approx(2_365_500_000)),
        ("Amarr VIII (Oris) - Emperor Family Academy", pytest.approx(180_000_000)),
    ]
    top = queries.value_map(conn, limit=1)
    assert [r["label"] for r in top] == ["Jita IV - Moon 4 - Caldari Navy Assembly Plant"]


# ------------------------------------------------------------- manual prices
def test_manual_price_survives_a_repricing_run(conn, monkeypatch):
    """A pinned price is the user overruling the market; the next automated
    pass replacing it would defeat the point of pinning. The pinned type must
    not even be asked for -- its Fuzzwork slot is wasted otherwise."""
    pricing.set_manual_price(conn, 20185, 3_500_000_000.0)
    requested: list[int] = []

    def fake_jita(ids, s, progress=None):
        requested.extend(ids)
        return {tid: pricing.Quote(buy=1.0, sell=2.0, source=pricing.JITA) for tid in ids}

    monkeypatch.setattr(pricing, "fetch_jita", fake_jita)
    pricing.refresh_prices(conn, _NoContractsClient(), Settings())

    row = conn.execute("SELECT * FROM prices WHERE type_id=20185").fetchone()
    assert row["source"] == "manual"
    assert (row["buy_price"], row["sell_price"]) == (3.5e9, 3.5e9)
    assert row["samples"] is None
    assert 20185 not in requested
    # The run itself really happened: an unpinned type took the new quote.
    other = conn.execute("SELECT * FROM prices WHERE type_id=645").fetchone()
    assert (other["buy_price"], other["sell_price"]) == (1.0, 2.0)


def test_store_prices_refuses_to_overwrite_a_manual_row(conn):
    """The guard sits on the single write path, not just in refresh_prices,
    so no future caller can clobber a pin by accident."""
    pricing.set_manual_price(conn, 34, 12.0)
    pricing.store_prices(conn, {
        34: pricing.Quote(buy=5.0, sell=5.5, source=pricing.JITA),
        645: pricing.Quote(buy=1.0, sell=2.0, source=pricing.JITA),
    })
    pinned = conn.execute("SELECT * FROM prices WHERE type_id=34").fetchone()
    assert pinned["source"] == "manual"
    assert (pinned["buy_price"], pinned["sell_price"]) == (12.0, 12.0)
    updated = conn.execute("SELECT * FROM prices WHERE type_id=645").fetchone()
    assert (updated["buy_price"], updated["sell_price"]) == (1.0, 2.0)


def test_clear_manual_price_restores_normal_repricing(conn, monkeypatch):
    pricing.set_manual_price(conn, 34, 99.0)
    pricing.clear_manual_price(conn, 34)
    assert conn.execute("SELECT * FROM prices WHERE type_id=34").fetchone() is None

    monkeypatch.setattr(
        pricing, "fetch_jita",
        lambda ids, s, progress=None: {34: pricing.Quote(buy=5.0, sell=5.5, source=pricing.JITA)},
    )
    pricing.refresh_prices(conn, _NoContractsClient(), Settings())
    row = conn.execute("SELECT * FROM prices WHERE type_id=34").fetchone()
    assert row["source"] == "jita"
    assert (row["buy_price"], row["sell_price"]) == (5.0, 5.5)


def test_clear_manual_price_leaves_market_rows_alone(conn):
    pricing.clear_manual_price(conn, 645)
    row = conn.execute("SELECT * FROM prices WHERE type_id=645").fetchone()
    assert row is not None and row["source"] == "jita"


def test_pinned_labels_and_saved_views_tables_exist(conn):
    """The Assets tab persists rail pins and saved views here; the schema
    must provide the tables and their uniqueness rules from a fresh init."""
    conn.execute("INSERT INTO pinned_labels VALUES ('location','Jita')")
    with pytest.raises(db.sqlite3.IntegrityError):
        conn.execute("INSERT INTO pinned_labels VALUES ('location','Jita')")
    conn.execute("INSERT INTO saved_views VALUES (1,'{}')")
    with pytest.raises(db.sqlite3.IntegrityError):
        conn.execute("INSERT INTO saved_views VALUES (1,'{}')")


def test_corp_blueprints_still_skip_gracefully_without_the_scope(conn):
    """Since we no longer request the scope, corp_blueprints must remain a
    soft skip (logged, not fatal) for characters who were granted it under an
    older version of this app and haven't re-authorised yet."""

    conn.execute(
        "INSERT INTO corporations (corporation_id, name) VALUES (2000, 'Test Corp')"
    )
    row = dict(
        character_id=100, name="Test Pilot", corporation_id=2000,
        corporation_name="Test Corp", include_corp=1,
    )

    class _Row(dict):
        def __getitem__(self, k):
            return super().__getitem__(k)

    s = _syncer(conn, _WalletClient())
    warnings = s._sync_corp(_Row(row), scopes=["esi-assets.read_corporation_assets.v1"], progress=None)
    assert any("corp blueprints" in w and "skipped" in w for w in warnings)
    assert any("corp divisions" in w and "skipped" in w for w in warnings)
    # The scope-gated pulls that do NOT depend on either broken scope must
    # still run -- corp assets was granted above and should not show as skipped.
    assert not any("corp assets" in w and "skipped" in w for w in warnings)
