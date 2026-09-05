"""The support report, and the failure that prompted it.

A first-time user synced 30 characters and 32,297 asset stacks, and the
Assets tab showed "0 of 32,297" with every column blank. Nothing on screen
said why. The cause was that the game data had never been imported: ASSET_ROWS
inner joins sde_types, so an empty SDE removes every asset before the table
sees one.

Two things are pinned here. That the join really does behave that way, so the
next person reading ASSET_ROWS does not have to reconstruct it, and that the
report names the cause in words rather than leaving a reader to spot a zero in
a column of numbers.
"""

from __future__ import annotations

import pytest

from evasset import db, diagnostics, queries
from evasset.config import Settings


@pytest.fixture()
def synced_but_no_sde(tmp_path):
    """The reported state: characters and assets present, SDE never imported."""
    conn = db.init(tmp_path / "nosde.sqlite")
    conn.execute(
        "INSERT INTO characters (character_id, name, enabled) VALUES (1, 'Test Pilot', 1)"
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO assets (item_id, owner_type, owner_id, type_id, quantity,"
            " location_id, root_location_id) VALUES (?,?,?,?,?,?,?)",
            (100 + i, "character", 1, 34, 1000, 60003760, 60003760),
        )
    return conn


def test_an_empty_sde_hides_every_asset(synced_but_no_sde):
    """The mechanism behind the bug report, stated once so it is not a
    surprise a second time."""
    conn = synced_but_no_sde
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0] == 0

    shown = conn.execute(f"SELECT COUNT(*) FROM ({queries.ASSET_ROWS})").fetchone()[0]
    assert shown == 0, "an inner join on sde_types drops every row"


def test_the_report_names_the_missing_sde(synced_but_no_sde):
    text = diagnostics.report(synced_but_no_sde, Settings())
    assert "PROBLEMS FOUND" in text
    assert "SDE" in text
    assert "Update -> Game data" in text


def test_the_report_flags_the_empty_table_inline(synced_but_no_sde):
    """A reader scanning row counts should not have to know which zero
    matters."""
    text = diagnostics.report(synced_but_no_sde, Settings())
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("sde_types"))
    assert "breaks the Assets tab" in line


def test_no_characters_is_reported_differently(tmp_path):
    """"Never set up" and "set up but broken" must not read the same."""
    conn = db.init(tmp_path / "empty.sqlite")
    text = diagnostics.report(conn, Settings())
    assert "No characters have been added" in text


def test_a_healthy_database_reports_no_problems(tmp_path):
    conn = db.init(tmp_path / "ok.sqlite")
    conn.execute("INSERT INTO characters (character_id, name, enabled) VALUES (1,'Test Pilot',1)")
    conn.executescript(
        """
        INSERT INTO sde_categories VALUES (4,'Material',1);
        INSERT INTO sde_groups VALUES (18,4,'Mineral',1);
        INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published)
          VALUES (34,'Tritanium',18,0.01,1,5,1);
        INSERT INTO assets (item_id,owner_type,owner_id,type_id,quantity,location_id,root_location_id)
          VALUES (1,'character',1,34,10,60003760,60003760);
        INSERT INTO prices (type_id,buy_price,sell_price,source,samples,updated_at)
          VALUES (34,3.76,3.99,'jita',1,'2026-01-01T00:00:00+00:00');
        """
    )
    assert diagnostics.problems(conn) == []
    assert "PROBLEMS FOUND" not in diagnostics.report(conn, Settings())


def test_the_report_never_contains_a_secret(tmp_path):
    """It exists to be pasted into a public issue."""
    conn = db.init(tmp_path / "s.sqlite")
    settings = Settings(client_secret="super-secret-value", contact_email="me@example.com")
    text = diagnostics.report(conn, settings)

    assert "super-secret-value" not in text
    assert "me@example.com" not in text, "the address is reported as set, not quoted"
    assert "client secret  set" in text


def test_the_report_shows_a_data_dir_override(tmp_path, monkeypatch):
    """An EVEBOOTY_DATA_DIR left in a shell is why "my data vanished" happens,
    and it is invisible from the UI."""
    monkeypatch.setenv("EVEBOOTY_DATA_DIR", str(tmp_path / "elsewhere"))
    conn = db.init(tmp_path / "o.sqlite")
    assert "override in effect" in diagnostics.report(conn, Settings())


def test_character_sync_errors_are_included(synced_but_no_sde):
    """last_error is where a lapsed sign-in is recorded, and it is the first
    thing worth reading after the row counts."""
    conn = synced_but_no_sde
    conn.execute("UPDATE characters SET last_error='Test Pilot: sign-in has expired'")
    assert "sign-in has expired" in diagnostics.report(conn, Settings())
