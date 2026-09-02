"""Structures that have been unanchored.

An unanchored structure does not come back from ESI marked as such -- it
simply stops appearing in /corporations/{id}/structures. Left alone, the row
stays owned=1 forever, still claiming the state and fuel_expires it had on the
day it went away, and a fuel clock frozen in the past reads as "out of fuel"
rather than "gone".

Marked rather than deleted, because the structures table is also how an asset
sitting in one gets a location name (ASSET_ROWS joins it). Deleting the row
renames somebody's hangar to "Unknown location 1048...", which these tests
pin down directly.
"""

from __future__ import annotations

import sqlite3

import pytest

from evasset import db, queries

SEED = """
INSERT INTO sde_regions VALUES (10000002,'The Forge');
INSERT INTO sde_systems VALUES (30000142,'Jita',20000020,10000002,0.9);
INSERT INTO sde_categories VALUES (65,'Structure',1);
INSERT INTO sde_groups VALUES (1657,65,'Citadel',1);
INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,published)
  VALUES (35832,'Astrahus',1657,1,1,1);
INSERT INTO characters (character_id,name,corporation_id,corporation_name,scopes,enabled)
  VALUES (100,'Test Pilot',2000,'Test Corp','x',1);
"""


def structure(conn, sid, name, *, owned=1, corp=2000, state="shield_vulnerable"):
    conn.execute(
        """INSERT INTO structures
             (structure_id,name,system_id,region_id,type_id,owner_id,
              resolved_at,accessible,owned,state,fuel_expires,updated_at)
           VALUES (?,?,30000142,10000002,35832,?, '2026-01-01',1,?,?,
                   '2026-01-05T00:00:00Z','2026-01-01T00:00:00Z')""",
        (sid, name, corp, owned, state),
    )


@pytest.fixture
def conn(tmp_path):
    c = db.init(tmp_path / "u.sqlite")
    c.executescript(SEED)
    return c


class FakeSyncer:
    """Just the marking half of Syncer, over a real connection.

    The real _corp_structures does an ESI round trip before it gets here;
    what is worth testing is what it does with the ids it came back with.
    """

    def __init__(self, conn):
        self.conn = conn

    def mark(self, corp_id, seen):
        from evasset.esi.sync import Syncer

        Syncer._mark_unanchored(self, corp_id, seen)


# ------------------------------------------------------------------- marking
def test_a_structure_esi_stopped_reporting_is_marked(conn):
    structure(conn, 1, "Still There")
    structure(conn, 2, "Unanchored One")

    FakeSyncer(conn).mark(2000, [1])

    rows = {r["structure_id"]: r["gone_at"] for r in conn.execute(
        "SELECT structure_id, gone_at FROM structures")}
    assert rows[1] is None
    assert rows[2] is not None


def test_nothing_is_marked_when_esi_returns_nothing(conn):
    """"This corp owns no structures" and "ESI handed back an empty page this
    once" are indistinguishable from here. Wrongly flagging an entire corp is
    much more expensive than leaving a genuinely emptied one listed."""
    structure(conn, 1, "One")
    structure(conn, 2, "Two")

    FakeSyncer(conn).mark(2000, [])

    gone = conn.execute(
        "SELECT COUNT(*) c FROM structures WHERE gone_at IS NOT NULL").fetchone()["c"]
    assert gone == 0


def test_another_corps_structures_are_left_alone(conn):
    """The response is per corp; a sync of corp A must not flag corp B."""
    structure(conn, 1, "Ours", corp=2000)
    structure(conn, 2, "Theirs", corp=3000)

    FakeSyncer(conn).mark(2000, [1])

    other = conn.execute(
        "SELECT gone_at FROM structures WHERE structure_id=2").fetchone()["gone_at"]
    assert other is None


def test_a_name_only_structure_is_never_marked(conn):
    """owned=0 rows are somebody else's Astrahus that one of our ships is
    parked in. They are never in our corp response, so they must not be
    treated as having vanished from it."""
    structure(conn, 9, "Someone Else's", owned=0, corp=None)

    FakeSyncer(conn).mark(2000, [1])

    assert conn.execute(
        "SELECT gone_at FROM structures WHERE structure_id=9").fetchone()["gone_at"] is None


def test_marking_twice_keeps_the_first_timestamp(conn):
    """gone_at is when it went, not when we last noticed it was gone."""
    structure(conn, 2, "Gone")
    FakeSyncer(conn).mark(2000, [1])
    first = conn.execute(
        "SELECT gone_at FROM structures WHERE structure_id=2").fetchone()["gone_at"]

    FakeSyncer(conn).mark(2000, [1])
    again = conn.execute(
        "SELECT gone_at FROM structures WHERE structure_id=2").fetchone()["gone_at"]
    assert again == first


def test_a_structure_that_comes_back_is_unmarked(conn):
    """An unanchor can be cancelled, and a bad sync can drop a page. Coming
    back clears the flag -- upsert_many is INSERT OR REPLACE, which rewrites
    the whole row, and gone_at is not among the columns it writes."""
    structure(conn, 2, "Flapping")
    FakeSyncer(conn).mark(2000, [1])
    assert conn.execute(
        "SELECT gone_at FROM structures WHERE structure_id=2").fetchone()["gone_at"]

    from evasset.esi.sync import Syncer
    db.upsert_many(conn, "structures", Syncer.STRUCTURE_COLS, [(
        2, "Flapping", 30000142, 10000002, 35832, 2000, "2026-02-01", 1, 1,
        "shield_vulnerable", None, None, "2026-03-01T00:00:00Z", None, None,
        None, None, "[]", "2026-02-01T00:00:00Z",
    )])
    assert conn.execute(
        "SELECT gone_at FROM structures WHERE structure_id=2").fetchone()["gone_at"] is None


# ------------------------------------------------------- the location joins
def test_assets_in_an_unanchored_structure_keep_their_location_name(conn):
    """The reason the row is marked instead of deleted."""
    structure(conn, 1048357853275, "Ikami - 0001 p2m1")
    conn.execute(
        """INSERT INTO assets (owner_type,owner_id,item_id,type_id,quantity,location_id,
             location_flag,location_type,is_singleton,root_location_id,system_id,region_id)
           VALUES ('character',100,1,35832,1,1048357853275,'Hangar','item',1,
                   1048357853275,30000142,10000002)"""
    )
    FakeSyncer(conn).mark(2000, [999])   # our structure vanishes from ESI

    row = queries.fetch_assets(conn)[0]
    assert row["location"] == "Ikami - 0001 p2m1"
    assert not row["location"].startswith("Unknown location")


def test_the_structures_query_exposes_gone_at(conn):
    """The view filters on it, so it has to come back from the query."""
    structure(conn, 2, "Gone")
    FakeSyncer(conn).mark(2000, [1])
    row = next(r for r in queries.fetch_structures(conn) if r["structure_id"] == 2)
    assert row["gone_at"]


# ----------------------------------------------------------------- migration
def test_an_older_database_gains_gone_at(tmp_path):
    """v3 databases have the column added in place rather than rebuilt: it is
    nullable with no default, so existing rows become NULL, which is the right
    answer ("still here as far as we know") until the next sync."""
    path = tmp_path / "old.sqlite"
    conn = db.init(path)
    conn.execute("ALTER TABLE structures DROP COLUMN gone_at")
    assert "gone_at" not in db.columns(conn, "structures")

    done = db.migrate(conn)
    assert "gone_at" in db.columns(conn, "structures")
    assert any("gone_at" in line for line in done)


def test_the_migration_is_idempotent(tmp_path):
    conn = db.init(tmp_path / "again.sqlite")
    assert not [line for line in db.migrate(conn) if "gone_at" in line]


# ------------------------------------------------------------ the UI predicate
def test_the_view_treats_both_kinds_as_unanchored():
    """ESI reporting the unanchored state, and ESI having stopped reporting it
    at all, are the same thing to someone reading the list."""
    pytest.importorskip("PySide6.QtWidgets")
    from evasset.ui.structures_view import is_unanchored

    def row(**kw):
        base = {"gone_at": None, "state": "shield_vulnerable"}
        base.update(kw)
        return base

    assert is_unanchored(row(gone_at="2026-01-01T00:00:00Z")) is True
    assert is_unanchored(row(state="unanchored")) is True
    assert is_unanchored(row(state="Unanchored")) is True     # case from ESI varies
    assert is_unanchored(row()) is False
    assert is_unanchored(row(state=None)) is False


def test_sqlite_rows_work_with_the_predicate(conn):
    """is_unanchored indexes by name; a sqlite3.Row is what it actually gets."""
    pytest.importorskip("PySide6.QtWidgets")
    from evasset.ui.structures_view import is_unanchored

    structure(conn, 2, "Gone")
    FakeSyncer(conn).mark(2000, [1])
    conn.row_factory = sqlite3.Row
    row = next(r for r in queries.fetch_structures(conn) if r["structure_id"] == 2)
    assert is_unanchored(row) is True


# --------------------------------------------------- what the row looks like
def _model_row(**kw):
    base = {
        "gone_at": None, "state": "shield_vulnerable",
        "fuel_expires": "2020-01-01T00:00:00Z", "services": "[]",
    }
    base.update(kw)
    return base


def test_an_unanchored_structure_is_not_an_emergency():
    """Its state, fuel clock and service list all froze the day it stopped
    being reported. Scoring them puts a permanent red row in the list
    demanding action nobody can take."""
    pytest.importorskip("PySide6.QtWidgets")
    from evasset.ui.structures_view import NORMAL, _StructuresModel

    model = _StructuresModel()
    live = _model_row()
    gone = _model_row(gone_at="2026-08-01T00:00:00Z")

    assert model.severity(live) != NORMAL      # long-expired fuel, so it would score
    assert model.severity(gone) == NORMAL


def test_the_state_column_says_unanchored_rather_than_the_frozen_state():
    pytest.importorskip("PySide6.QtWidgets")
    from evasset.ui.structures_view import _StructuresModel

    model = _StructuresModel()
    assert model.display(_model_row(gone_at="2026-08-01T00:00:00Z"), "state") == "Unanchored"
    assert model.display(_model_row(), "state") == "Shield vulnerable"


def test_frozen_deadlines_are_blanked_rather_than_counted_down():
    """A fuel_expires from the day it vanished counts down to a date nothing
    will ever refresh, and renders as an imminent deadline."""
    pytest.importorskip("PySide6.QtWidgets")
    from evasset.ui.structures_view import _StructuresModel

    model = _StructuresModel()
    assert model.display(_model_row(gone_at="2026-08-01T00:00:00Z"), "fuel_expires") == ""
    assert model.display(_model_row(), "fuel_expires") != ""
