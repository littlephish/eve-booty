"""What the Overview and History tabs read.

One structure's row, with the owning corporation's alliance attached, and
that structure's recorded changes in order. Both are plain queries; the
judgement about how to word a change lives in evasset.structure_history.
"""

from __future__ import annotations

import pytest

from evasset import db, queries

CORP = 98000001
SID = 1048466934500

SEED = """
INSERT INTO sde_regions VALUES (10000002,'The Forge');
INSERT INTO sde_systems VALUES (30000142,'Ikami',20000020,10000002,0.5);
INSERT INTO sde_categories VALUES (65,'Structure',1);
INSERT INTO sde_groups VALUES (1657,65,'Refinery',1);
INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,published)
  VALUES (35835,'Athanor',1657,1,1,1);
INSERT INTO corporations (corporation_id,name,ticker,alliance_id,alliance_name)
  VALUES (98000001,'Work Hard for My Money','DOLRS',99000001,'Krill Issue Holdings');
INSERT INTO characters (character_id,name,corporation_id,corporation_name,scopes,enabled)
  VALUES (100,'Test Pilot',98000001,'Work Hard for My Money','x',1);
INSERT INTO structures
  (structure_id,name,system_id,region_id,type_id,owner_id,resolved_at,accessible,
   owned,state,fuel_expires,services,updated_at)
  VALUES (1048466934500,'Ikami - 0064 p11m1',30000142,10000002,35835,98000001,
          '2026-01-01',1,1,'shield_vulnerable','2026-09-24T04:00:00Z',
          '[{"name":"Moon Drilling","state":"online"}]','2026-09-05T13:42:00Z');
"""


@pytest.fixture
def conn(tmp_path):
    c = db.init(tmp_path / "ov.sqlite")
    c.executescript(SEED)
    return c


def add_change(conn, field, old, new, at):
    conn.execute(
        "INSERT INTO structure_changes (structure_id, observed_at, field,"
        " old_value, new_value) VALUES (?,?,?,?,?)",
        (SID, at, field, old, new),
    )


# ----------------------------------------------------------------- overview
def test_one_structure_comes_back_by_id(conn):
    row = queries.fetch_structure(conn, SID)
    assert row["name"] == "Ikami - 0064 p11m1"


def test_the_overview_carries_what_it_displays(conn):
    row = queries.fetch_structure(conn, SID)
    assert row["type_name"] == "Athanor"
    assert row["system_name"] == "Ikami"
    assert row["region_name"] == "The Forge"
    assert row["owner_name"] == "Work Hard for My Money"


def test_the_overview_names_the_alliance(conn):
    """From the corporation, not the character -- a structure belongs to a
    corp, and that corp's alliance is the one on the screen."""
    assert queries.fetch_structure(conn, SID)["alliance_name"] == "Krill Issue Holdings"


def test_a_structure_with_no_alliance_is_not_an_error(conn):
    """Plenty of corps are in no alliance at all."""
    conn.execute("UPDATE corporations SET alliance_id=NULL, alliance_name=NULL")
    assert queries.fetch_structure(conn, SID)["alliance_name"] is None


def test_the_type_id_is_there_for_the_render(conn):
    assert queries.fetch_structure(conn, SID)["type_id"] == 35835


def test_an_unknown_structure_is_none_rather_than_an_error(conn):
    assert queries.fetch_structure(conn, 1) is None


def test_first_seen_comes_from_the_history(conn):
    """There is no anchored-at column and never can be. The earliest thing
    recorded about a structure is the closest honest answer."""
    add_change(conn, "tracking", None, "started", "2026-08-21T13:43:00Z")
    add_change(conn, "state", "a", "b", "2026-09-05T13:42:00Z")
    assert queries.fetch_structure(conn, SID)["first_seen"] == "2026-08-21T13:43:00Z"


def test_first_seen_is_empty_before_anything_is_recorded(conn):
    assert queries.fetch_structure(conn, SID)["first_seen"] is None


# ------------------------------------------------------------------ history
def test_a_structures_changes_come_back(conn):
    add_change(conn, "state", "shield_vulnerable", "armor_reinforce", "2026-09-05T13:42:00Z")
    rows = queries.fetch_structure_changes(conn, SID)
    assert [r["field"] for r in rows] == ["state"]


def test_another_structures_changes_are_not_included(conn):
    add_change(conn, "state", "a", "b", "2026-09-05T13:42:00Z")
    conn.execute(
        "INSERT INTO structure_changes (structure_id, observed_at, field, new_value)"
        " VALUES (?,?,?,?)",
        (999, "2026-09-05T13:42:00Z", "state", "x"),
    )
    assert len(queries.fetch_structure_changes(conn, SID)) == 1


def test_changes_come_back_with_what_the_wording_needs(conn):
    add_change(conn, "fuel_expires", "2026-09-24T04:00:00Z", "2026-11-02T04:00:00Z",
               "2026-09-05T13:42:00Z")
    row = queries.fetch_structure_changes(conn, SID)[0]
    assert row["observed_at"] == "2026-09-05T13:42:00Z"
    assert row["old_value"] == "2026-09-24T04:00:00Z"
    assert row["new_value"] == "2026-11-02T04:00:00Z"


def test_no_history_is_an_empty_list(conn):
    assert queries.fetch_structure_changes(conn, SID) == []


# ------------------------------------------------------- resolving the alliance
# alliance_id arrives in the /corporations/{id} body the character sync
# already fetches; only the name needs a call, and that one is public.
class FakeClient:
    def __init__(self, bodies):
        self.bodies = bodies
        self.paths: list[str] = []

    def get(self, path, **kw):
        self.paths.append(path)
        return self.bodies.get(path)


def register(conn, bodies):
    from evasset.esi.sync import Syncer

    client = FakeClient(bodies)
    Syncer(conn, client, None).register_character(100, "Test Pilot", ["publicData"])
    return client


BODIES = {
    "/characters/100": {"corporation_id": CORP, "alliance_id": 99000001},
    f"/corporations/{CORP}": {
        "name": "Work Hard for My Money", "ticker": "DOLRS", "alliance_id": 99000001,
    },
    "/alliances/99000001": {"name": "Krill Issue Holdings"},
}


def test_registering_stores_the_corps_alliance(conn):
    conn.execute("UPDATE corporations SET alliance_id=NULL, alliance_name=NULL")
    register(conn, BODIES)
    row = conn.execute("SELECT * FROM corporations WHERE corporation_id=?", (CORP,)).fetchone()
    assert row["alliance_id"] == 99000001
    assert row["alliance_name"] == "Krill Issue Holdings"


def test_a_corp_in_no_alliance_asks_nobody(conn):
    """No alliance_id means no lookup to make -- and no wasted request."""
    bodies = dict(BODIES)
    bodies["/characters/100"] = {"corporation_id": CORP}
    bodies[f"/corporations/{CORP}"] = {"name": "Solo Corp", "ticker": "SOLO"}
    client = register(conn, bodies)
    assert not [p for p in client.paths if p.startswith("/alliances/")]


def test_an_alliance_lookup_that_fails_does_not_stop_registration(conn):
    """The name is decoration on one tab. Losing a character over it would
    not be a trade worth making."""
    bodies = dict(BODIES)
    bodies["/alliances/99000001"] = None  # what allow_404 yields
    register(conn, bodies)
    row = conn.execute("SELECT * FROM corporations WHERE corporation_id=?", (CORP,)).fetchone()
    assert row["alliance_id"] == 99000001
    assert row["alliance_name"] is None
    assert conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 1
