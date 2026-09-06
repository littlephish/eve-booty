"""Storage for a structure's history, and the migration that adds it.

structure_changes holds raw field changes -- "fuel_expires went from A to B"
-- and nothing about what they mean. The wording lives in
evasset.structure_history, at read time, on purpose: the rules for turning a
change into a sentence are judgement calls that will be revised (whether a
forward fuel jump is really "Refuelled", for one), and a database of raw
facts re-renders under a new rule where a database of finished sentences does
not.

corporations gains the alliance columns in the same version. alliance_id is
already in the /corporations/{id} response the character sync fetches and
throws away; only the name needs a call of its own.
"""

from __future__ import annotations

import sqlite3

import pytest

from evasset import db


@pytest.fixture()
def conn(tmp_path):
    return db.init(tmp_path / "h.sqlite")


# --------------------------------------------------------------- new table
def test_a_fresh_database_has_somewhere_to_put_changes(conn):
    assert db._table_exists(conn, "structure_changes")


def test_a_change_row_records_the_field_and_both_sides(conn):
    conn.execute(
        "INSERT INTO structure_changes (structure_id, observed_at, field,"
        " old_value, new_value) VALUES (?,?,?,?,?)",
        (1234, "2026-09-05T13:42:00Z", "fuel_expires",
         "2026-09-24T04:00:00Z", "2026-11-02T04:00:00Z"),
    )
    row = conn.execute("SELECT * FROM structure_changes").fetchone()
    assert row["structure_id"] == 1234
    assert row["field"] == "fuel_expires"
    assert row["old_value"] == "2026-09-24T04:00:00Z"
    assert row["new_value"] == "2026-11-02T04:00:00Z"


def test_the_first_sighting_has_no_previous_value(conn):
    """A structure seen for the first time gets one 'tracking' row, and there
    is no old value to record for it."""
    conn.execute(
        "INSERT INTO structure_changes (structure_id, observed_at, field, new_value)"
        " VALUES (?,?,?,?)",
        (1234, "2026-09-05T13:42:00Z", "tracking", "started"),
    )
    assert conn.execute("SELECT old_value FROM structure_changes").fetchone()[0] is None


def test_changes_are_indexed_by_the_structure_they_belong_to(conn):
    """The History tab reads one structure's changes in time order, and that
    is the only way anything reads this table."""
    names = {r["name"] for r in conn.execute("PRAGMA index_list(structure_changes)")}
    assert any("structure_changes" in n for n in names)


# ------------------------------------------------------------ corporations
def test_corporations_can_name_an_alliance(conn):
    assert {"alliance_id", "alliance_name"} <= db.columns(conn, "corporations")


# ---------------------------------------------------------------- upgrading
def old_database(path):
    """A v5 corporations table -- the shape before this change."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE corporations (
            corporation_id   INTEGER PRIMARY KEY,
            name             TEXT,
            ticker           TEXT,
            via_character_id INTEGER,
            last_sync_at     TEXT
        );
        INSERT INTO corporations VALUES (98000001,'Work Hard for My Money','DOLRS',90001,'2026-09-01T00:00:00Z');
        INSERT INTO meta VALUES ('schema_version','5');
        """
    )
    conn.commit()
    conn.close()


def test_upgrading_keeps_the_corporations_already_there(tmp_path):
    """These rows are what stop the app re-asking ESI for a corp's name, so
    the migration rebuilds rather than starting fresh."""
    path = tmp_path / "old.sqlite"
    old_database(path)
    conn = db.init(path)
    row = conn.execute("SELECT * FROM corporations").fetchone()
    assert row["name"] == "Work Hard for My Money"
    assert row["ticker"] == "DOLRS"
    assert row["via_character_id"] == 90001


def test_upgrading_adds_the_alliance_columns_empty(tmp_path):
    """Nullable, and left NULL: an existing row has no alliance recorded and
    will not until the next character sync."""
    path = tmp_path / "old.sqlite"
    old_database(path)
    conn = db.init(path)
    row = conn.execute("SELECT * FROM corporations").fetchone()
    assert row["alliance_id"] is None
    assert row["alliance_name"] is None


def test_upgrading_twice_is_harmless(tmp_path):
    """init() runs on every launch; the migration has to be a no-op the
    second time."""
    path = tmp_path / "old.sqlite"
    old_database(path)
    db.init(path)
    conn = db.init(path)  # not closed in between: db.connect caches per thread
    assert conn.execute("SELECT COUNT(*) FROM corporations").fetchone()[0] == 1
    assert db.migrate(conn) == []  # nothing left to do


def test_the_schema_version_moved_on(tmp_path):
    path = tmp_path / "old.sqlite"
    old_database(path)
    conn = db.init(path)
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)
    assert db.SCHEMA_VERSION >= 6
