"""The Container column: what a row is nested inside.

The Slot column answers this whenever the compartment is a property of the row
itself -- CorpSAG1 for a corp hangar division, HiSlot0 for a fitting. It cannot
answer it when the compartment belongs to an ancestor: an item inside an asset
safety wrap carries its own flag of "Hangar", because it is in the hangar of
the wrap, and the wrap is what you actually wanted to know about.

A path rather than just the immediate parent, because the nesting is real: a
fuel can in a capital's hangar, a can in a corp division, a ship inside an
asset safety wrap. Naming only the innermost container answers "which can"
while losing "which ship", which is the same question asked twice.
"""

from __future__ import annotations

import pytest

from evasset import db, queries

STATION = 60003760


def add(conn, item_id, type_id, location_id, flag, name=None):
    conn.execute(
        "INSERT INTO assets (owner_type, owner_id, item_id, type_id, quantity,"
        " location_id, location_flag, location_type, is_singleton,"
        " is_blueprint_copy, custom_name, root_location_id, system_id, region_id)"
        " VALUES ('character',100,?,?,1,?,?,'item',1,0,?,?,30000142,10000002)",
        (item_id, type_id, location_id, flag, name, STATION),
    )


@pytest.fixture()
def conn(tmp_path):
    c = db.init(tmp_path / "cp.sqlite")
    c.executescript(
        """
        INSERT INTO sde_categories VALUES (6,'Ship',1),(4,'Material',1),(0,'Abstract',1);
        INSERT INTO sde_groups VALUES (27,6,'Battleship',1),(18,4,'Mineral',1),
                                      (1319,0,'Miscellaneous',1),(12,4,'Cargo Container',1);
        INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published)
          VALUES (60,'Asset Safety Wrap',1319,0,1,0,1),
                 (645,'Dominix',27,454500,1,1,1),
                 (34,'Tritanium',18,0.01,1,5,1),
                 (3296,'Station Container',12,1000,1,1,1);
        INSERT INTO sde_stations VALUES (60003760,'Jita IV - Moon 4',30000142,10000002);
        INSERT INTO sde_systems VALUES (30000142,'Jita',20000020,10000002,0.9);
        INSERT INTO sde_regions VALUES (10000002,'The Forge');
        """
    )
    add(c, 1, 34, STATION, "Hangar")                      # loose in the hangar
    add(c, 2, 3296, STATION, "Hangar", "Ore Can 3")       # a named can
    add(c, 3, 34, 2, "Unlocked")                          # inside that can
    add(c, 4, 60, STATION, "AssetSafety")                 # a wrap
    add(c, 5, 645, 4, "Hangar", "L2 HMS Dragoon")         # a ship in the wrap
    add(c, 6, 34, 5, "Cargo")                             # cargo of that ship
    return c


def container(conn, item_id):
    row = conn.execute(
        queries.ASSET_ROWS + " WHERE a.item_id = ?", (item_id,)
    ).fetchone()
    return row["container"]


def test_a_loose_item_has_no_container(conn):
    """Blank for the common case, rather than repeating the station on every
    row: that is what the Location column already says."""
    assert container(conn, 1) is None


def test_an_item_in_a_can_names_the_can(conn):
    assert container(conn, 3) == "Ore Can 3"


def test_the_containers_custom_name_wins(conn):
    """"in Ore Can 3" beats "in Station Container" for finding it again."""
    assert "Station Container" not in (container(conn, 3) or "")


def test_a_wrap_shows_as_a_container(conn):
    assert container(conn, 5) == "Asset Safety Wrap"


def test_nesting_reads_outermost_first(conn):
    """The case the immediate parent alone could not answer: which wrap, and
    which ship inside it."""
    assert container(conn, 6) == "Asset Safety Wrap > L2 HMS Dragoon"


def test_a_container_is_only_matched_within_its_owner(conn):
    """item_ids are unique in EVE, but the assets primary key is
    (owner_type, owner_id, item_id) and the join follows it."""
    conn.execute(
        "INSERT INTO assets (owner_type, owner_id, item_id, type_id, quantity,"
        " location_id, location_flag, location_type, is_singleton,"
        " is_blueprint_copy, custom_name, root_location_id, system_id, region_id)"
        " VALUES ('corporation',999,7,34,1,2,'Unlocked','item',1,0,NULL,?,30000142,10000002)",
        (STATION,),
    )
    row = conn.execute(
        queries.ASSET_ROWS + " WHERE a.item_id = 7 AND a.owner_type='corporation'"
    ).fetchone()
    assert row["container"] is None, "the corp row must not borrow a character's can"


def test_the_walk_is_depth_capped(conn):
    """A container cycle should be impossible, but the flattening resolver
    already had to be hardened against one, and an unbounded recursive CTE
    meeting a cycle does not return."""
    assert "w.depth < 8" in queries.ASSET_ROWS


def test_the_column_is_offered_to_the_table(conn):
    assert ("container", "Container") in queries.ASSET_COLUMNS
