"""Asset safety, as it actually arrives rather than as documented.

ESI documents location_id 2004 as the asset safety location, and the filter
was written against it. On a real account holding 18,263 assets that clause
matched zero rows, while 213 asset safety wraps sat there holding 2,483 items.

2004 appears to describe the in-transit case, while the safety timer runs.
Once delivered, the items are an "Asset Safety Wrap" (type 60) parked in a
real NPC station, carrying location_flag='AssetSafety', with the contents as
ordinary child rows underneath it. Both are real; only one was handled.

The nesting is two levels deep, not one, because a ship inside a wrap keeps
its fitted modules inside the ship. Matching direct children alone missed
1,539 rows on that same account.
"""

from __future__ import annotations

import pytest

from evasset import db, omni, queries
from evasset.config import ASSET_SAFETY_LOCATION_ID

STATION = 60003760


def insert(conn, item_id, type_id, location_id, flag, *, qty=1, root=STATION):
    conn.execute(
        "INSERT INTO assets (owner_type, owner_id, item_id, type_id, quantity,"
        " location_id, location_flag, location_type, is_singleton,"
        " is_blueprint_copy, custom_name, root_location_id, system_id, region_id)"
        " VALUES ('character',100,?,?,?,?,?,'item',1,0,NULL,?,30000142,10000002)",
        (item_id, type_id, qty, location_id, flag, root),
    )


@pytest.fixture()
def conn(tmp_path):
    """A wrap at a station, an item in it, and a module fitted to a ship in it."""
    c = db.init(tmp_path / "safety.sqlite")
    c.executescript(
        """
        INSERT INTO sde_categories VALUES (6,'Ship',1),(4,'Material',1),
                                          (0,'Abstract',1),(7,'Module',1);
        INSERT INTO sde_groups VALUES (27,6,'Battleship',1),(18,4,'Mineral',1),
                                      (1319,0,'Miscellaneous',1),(52,7,'Warp Scrambler',1);
        INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published)
          VALUES (60,'Asset Safety Wrap',1319,0,1,0,1),
                 (645,'Dominix',27,454500,1,150000000,1),
                 (34,'Tritanium',18,0.01,1,5,1),
                 (2873,'Warp Disruptor II',52,5,1,1000000,1);
        INSERT INTO sde_stations VALUES (60003760,'Jita IV - Moon 4',30000142,10000002);
        INSERT INTO sde_systems VALUES (30000142,'Jita',20000020,10000002,0.9);
        INSERT INTO sde_regions VALUES (10000002,'The Forge');
        """
    )
    # The wrap: an item at a station, flagged AssetSafety.
    insert(c, 900, 60, STATION, "AssetSafety")
    # Directly inside the wrap.
    insert(c, 901, 34, 900, "Hangar", qty=500)
    # A ship inside the wrap...
    insert(c, 902, 645, 900, "Hangar")
    # ...with a module fitted to that ship: two levels below the wrap.
    insert(c, 903, 2873, 902, "HiSlot0")
    # Something unrelated at the same station.
    insert(c, 904, 34, STATION, "Hangar", qty=10)
    return c


def _ids(conn, query: str) -> set[int]:
    """item_ids a query returns. Names collide when the same type sits in more
    than one place, which it does throughout these fixtures."""
    spec = omni.parse(query)
    where, params = spec.where()
    sql = queries.ASSET_ROWS + (f" WHERE {where}" if where else "")
    return {r["item_id"] for r in conn.execute(sql, params)}


def matching(conn, query: str) -> set[str]:
    """Item names for a query. Fine for "is this in the set" assertions."""
    return {r["item"] for r in _rows(conn, query)}


def _rows(conn, query: str):
    spec = omni.parse(query)
    where, params = spec.where()
    sql = queries.ASSET_ROWS + (f" WHERE {where}" if where else "")
    return list(conn.execute(sql, params))


def ids(conn, query: str) -> set[int]:
    """item_ids, for anything counting rows. The same type appears both inside
    and outside a wrap, so names would collide."""
    return {r["item_id"] for r in _rows(conn, query)}


def test_the_wrap_itself_is_in_asset_safety(conn):
    assert "Asset Safety Wrap" in matching(conn, "is:safety")


def test_items_inside_the_wrap_are_too(conn):
    """This is the case the old clause missed entirely."""
    assert "Tritanium" in matching(conn, "is:safety")


def test_a_module_fitted_to_a_ship_inside_the_wrap_is_too(conn):
    """Two levels deep. Matching only direct children of the wrap missed
    1,539 rows on a real account."""
    assert "Warp Disruptor II" in matching(conn, "is:safety")


def test_unrelated_items_at_the_same_station_are_not(conn):
    """The wrap sits in an ordinary station, so 'at this station' cannot be
    the test -- everything else there would come with it."""
    assert ids(conn, "is:safety") == {900, 901, 902, 903}, "wrap plus its three contents"
    assert 904 not in ids(conn, "is:safety")


def test_negation_returns_everything_else(conn):
    """item_id is the primary key and never NULL, so NOT IN cannot silently
    drop rows the way the old NULL-valued column comparison could."""
    inside = ids(conn, "is:safety")
    outside = ids(conn, "-is:safety")

    assert not (inside & outside), "a row cannot be both"
    total = {r["item_id"] for r in conn.execute(queries.ASSET_ROWS)}
    assert inside | outside == total, "every row must land on one side"


def test_the_in_transit_location_still_matches(conn):
    """2004 is documented and does occur while the timer runs. Fixing the
    common case must not drop the one that was already handled."""
    insert(conn, 905, 34, ASSET_SAFETY_LOCATION_ID, "AssetSafety",
           root=ASSET_SAFETY_LOCATION_ID)
    rows = [r for r in conn.execute(
        queries.ASSET_ROWS + " WHERE " + omni._IS_SQL["safety"][0])]
    assert any(r["item_id"] == 905 for r in rows)


def test_it_combines_with_other_chips(conn):
    """An is: flag that cannot be narrowed is not much of a filter."""
    assert ids(conn, "is:safety cat:Ship") == {902}, "the ship in the wrap"
    assert ids(conn, "is:safety Tritanium") == {901}, "the wrapped stack, not the loose one"


# ---------------------------------------------------------------- deliveries
# Same shape as asset safety: the compartment is on the row sitting in the
# location, and its contents are ordinary child rows with ordinary flags. A
# delivered ship arrives with its fitting, so this walks too.
def test_a_personal_delivery_is_found(conn):
    insert(conn, 910, 34, STATION, "Deliveries", qty=50)
    assert 910 in _ids(conn, "is:delivery")


def test_a_corp_delivery_is_found(conn):
    conn.execute(
        "INSERT INTO assets (owner_type, owner_id, item_id, type_id, quantity,"
        " location_id, location_flag, location_type, is_singleton,"
        " is_blueprint_copy, custom_name, root_location_id, system_id, region_id)"
        " VALUES ('corporation',200,911,34,5,?,'CorpDeliveries','item',1,0,NULL,?,30000142,10000002)",
        (STATION, STATION),
    )
    assert 911 in _ids(conn, "is:delivery")


def test_the_contents_of_a_delivery_are_found(conn):
    """A delivered ship arrives fitted; the modules carry their own slot flags
    and would be missed by matching the flag alone."""
    insert(conn, 912, 645, STATION, "Deliveries")     # a ship being delivered
    insert(conn, 913, 2873, 912, "HiSlot0")           # fitted to it
    found = _ids(conn, "is:delivery")
    assert {912, 913} <= found


def test_all_four_esi_delivery_flags_are_matched(conn):
    """Two of these appear in one real account; the others are in ESI's enum
    and would otherwise be silently ignored until somebody had one."""
    from evasset.omni import DELIVERY_FLAGS

    assert set(DELIVERY_FLAGS) == {
        "Deliveries", "CorpDeliveries", "CapsuleerDeliveries", "CorporationGoalDeliveries",
    }
    for i, flag in enumerate(DELIVERY_FLAGS):
        insert(conn, 920 + i, 34, STATION, flag)
    assert {920 + i for i in range(len(DELIVERY_FLAGS))} <= _ids(conn, "is:delivery")


def test_an_ordinary_hangar_item_is_not_a_delivery(conn):
    assert 904 not in _ids(conn, "is:delivery")


def test_delivery_negation_splits_every_row(conn):
    insert(conn, 930, 34, STATION, "Deliveries")
    inside = _ids(conn, "is:delivery")
    outside = _ids(conn, "-is:delivery")
    assert not (inside & outside)
    total = {r["item_id"] for r in conn.execute(queries.ASSET_ROWS)}
    assert inside | outside == total


def test_delivery_is_offered_as_a_flag():
    """The chip builder and the completer both read IS_FLAGS, so a flag that
    works but is not listed is a flag nobody finds."""
    assert "delivery" in omni.IS_FLAGS
