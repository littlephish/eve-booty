"""Structures tab: deadline formatting, severity, and the schema migration.

The formatting helpers are pure functions taking an explicit `now`, which is
the only reason any of this is testable -- a countdown that reads the clock
itself can only be asserted against approximately.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from evasset import db, queries
from evasset.ui import structures_view as sv

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def iso(**delta) -> str:
    return (NOW + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ parsing
def test_parses_the_trailing_z_esi_actually_sends():
    """fromisoformat only learned to accept Z in 3.11 and this supports 3.10."""
    assert sv.parse_utc("2026-08-24T18:30:00Z") == datetime(
        2026, 8, 24, 18, 30, tzinfo=timezone.utc
    )


def test_a_naive_timestamp_is_read_as_utc_not_local():
    """Guessing local here would shift every timer by the viewer's offset."""
    assert sv.parse_utc("2026-08-24T18:30:00") == datetime(
        2026, 8, 24, 18, 30, tzinfo=timezone.utc
    )


def test_an_offset_timestamp_is_normalised_to_utc():
    assert sv.parse_utc("2026-08-24T20:30:00+02:00") == datetime(
        2026, 8, 24, 18, 30, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("value", [None, "", "not a date", "2026-13-45T99:99:99Z"])
def test_unparseable_values_are_blank_rather_than_raising(value):
    assert sv.parse_utc(value) is None
    assert sv.fmt_deadline(value, NOW) == ""


# --------------------------------------------------------------- countdowns
@pytest.mark.parametrize(
    "delta,expected",
    [
        ({"days": 3, "hours": 6}, "3d 6h"),
        ({"hours": 1, "minutes": 5}, "1h 5m"),
        ({"minutes": 5}, "5m"),
        ({"seconds": 20}, "now"),
        ({"hours": -2}, "passed"),
    ],
)
def test_remaining_reads_at_the_granularity_people_plan_at(delta, expected):
    assert sv.fmt_remaining(timedelta(**delta)) == expected


def test_deadline_shows_absolute_and_remaining_together():
    """Absolute alone makes you do the arithmetic; remaining alone is no use
    for agreeing a time with anyone."""
    text = sv.fmt_deadline(iso(days=3, hours=6), NOW)
    assert "2026-08-24 18:00" in text
    assert "3d 6h" in text


def test_missing_deadlines_sort_last():
    """A structure with no timer is not the most urgent thing on the screen."""
    assert sv.sort_key(None) == float("inf")
    assert sv.sort_key(iso(hours=1)) < sv.sort_key(iso(days=1))


# ----------------------------------------------------------------- severity
@pytest.mark.parametrize("state", ["armor_reinforce", "hull_reinforce"])
def test_reinforcement_is_critical(state):
    assert sv.state_severity(state) == sv.CRITICAL


@pytest.mark.parametrize("state", ["shield_vulnerable", "armor_vulnerable"])
def test_vulnerability_is_a_warning(state):
    assert sv.state_severity(state) == sv.WARN


@pytest.mark.parametrize("state", ["anchoring", "unanchored", None, "unknown"])
def test_everything_else_is_normal(state):
    assert sv.state_severity(state) == sv.NORMAL


@pytest.mark.parametrize(
    "delta,expected",
    [
        ({"days": 10}, sv.NORMAL),
        ({"days": 2}, sv.WARN),
        ({"hours": 6}, sv.CRITICAL),
        ({"hours": -1}, sv.CRITICAL),
    ],
)
def test_fuel_gets_louder_as_it_runs_out(delta, expected):
    assert sv.fuel_severity(iso(**delta), NOW) == expected


def test_no_fuel_timestamp_is_not_treated_as_an_empty_bay():
    """A structure ESI reports no fuel_expires for -- an unanchored one, say --
    must not show up as critically low."""
    assert sv.fuel_severity(None, NOW) == sv.NORMAL


# ----------------------------------------------------------------- services
def test_services_summarise_online_and_offline():
    raw = json.dumps([
        {"name": "Clone Bay", "state": "online"},
        {"name": "Market Hub", "state": "offline"},
    ])
    assert sv.fmt_services(raw) == "1 online · 1 offline"
    assert sv.services_severity(raw) == sv.WARN


def test_all_online_is_not_a_warning():
    raw = json.dumps([{"name": "Clone Bay", "state": "online"}])
    assert sv.services_severity(raw) == sv.NORMAL


@pytest.mark.parametrize("raw", [None, "", "[]", "not json"])
def test_bad_or_absent_service_json_is_survivable(raw):
    assert sv.fmt_services(raw) == ""
    assert sv.services_severity(raw) == sv.NORMAL


# ------------------------------------------------------- vulnerability hour
def test_vuln_window_is_the_hour_esi_gives_not_an_invented_span():
    assert sv.fmt_vuln_window(18) == "18:00"


def test_a_pending_reinforce_hour_change_is_shown_with_when_it_applies():
    text = sv.fmt_vuln_window(18, 21, "2026-09-01T00:00:00Z")
    assert "18:00" in text and "21:00" in text and "2026-09-01" in text


def test_no_pending_change_when_the_next_hour_matches():
    assert sv.fmt_vuln_window(18, 18, "2026-09-01T00:00:00Z") == "18:00"


def test_no_reinforce_hour_is_blank():
    assert sv.fmt_vuln_window(None) == ""


# ---------------------------------------------------------------- migration
def _v2_structures_table(path):
    """The shape structures had before the Structures tab existed."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """CREATE TABLE structures (
               structure_id INTEGER PRIMARY KEY,
               name         TEXT,
               system_id    INTEGER,
               region_id    INTEGER,
               type_id      INTEGER,
               owner_id     INTEGER,
               resolved_at  TEXT,
               accessible   INTEGER NOT NULL DEFAULT 1
           );
           INSERT INTO structures VALUES
               (1035466617946, 'V-3YG7 Keepstar', 30000142, 10000002, 35834, 98000001,
                '2026-08-01T00:00:00Z', 1),
               (1035466617947, NULL, 30000142, 10000002, NULL, NULL,
                '2026-08-01T00:00:00Z', 0);"""
    )
    conn.commit()
    conn.close()


def test_migration_keeps_existing_structure_rows(tmp_path):
    """Those rows are what stops the app re-asking ESI for the name of every
    structure it has ever seen, including ones it cannot dock in."""
    path = tmp_path / "old.sqlite"
    _v2_structures_table(path)

    conn = db.init(path)

    rows = {r["structure_id"]: r for r in conn.execute("SELECT * FROM structures")}
    assert len(rows) == 2
    assert rows[1035466617946]["name"] == "V-3YG7 Keepstar"
    assert rows[1035466617947]["accessible"] == 0, "the no-access marker must survive"


def test_migration_adds_the_operational_columns_as_empty(tmp_path):
    path = tmp_path / "old.sqlite"
    _v2_structures_table(path)

    conn = db.init(path)

    cols = db.columns(conn, "structures")
    assert {"state", "fuel_expires", "state_timer_end", "services", "owned"} <= cols
    row = conn.execute(
        "SELECT * FROM structures WHERE structure_id=1035466617946"
    ).fetchone()
    assert row["state"] is None
    assert row["owned"] == 0, "a name-lookup row must not claim to be one of ours"


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "old.sqlite"
    _v2_structures_table(path)
    db.init(path)
    conn = db.init(path)          # second run must find nothing left to do
    assert conn.execute("SELECT COUNT(*) c FROM structures").fetchone()["c"] == 2


# -------------------------------------------------------------------- query
def test_fetch_structures_lists_only_the_ones_we_own(tmp_path):
    """The same table holds structures seen only as a location -- somebody
    else's Astrahus a ship is parked in -- and those have no fuel, state or
    timer, so they would be pages of blank rows."""
    conn = db.init(tmp_path / "s.sqlite")
    conn.executescript(
        """INSERT INTO sde_regions VALUES (10000002,'The Forge');
           INSERT INTO sde_systems VALUES (30000142,'Jita',20000020,10000002,0.9);
           INSERT INTO sde_categories VALUES (65,'Structure',1);
           INSERT INTO sde_groups VALUES (1657,65,'Citadel',1);
           INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,base_price,published)
               VALUES (35834,'Keepstar',1657,10000,1,0,1);"""
    )
    conn.execute(
        "INSERT INTO structures(structure_id,name,system_id,region_id,type_id,owner_id,"
        "accessible,owned,state,fuel_expires) VALUES(1,'Ours',30000142,10000002,35834,"
        "98000001,1,1,'shield_vulnerable',?)",
        (iso(days=5),),
    )
    conn.execute(
        "INSERT INTO structures(structure_id,name,system_id,region_id,type_id,accessible,owned)"
        " VALUES(2,'Someone elses',30000142,10000002,35834,1,0)"
    )

    rows = queries.fetch_structures(conn)

    assert [r["name"] for r in rows] == ["Ours"]
    assert rows[0]["type_name"] == "Keepstar"
    assert rows[0]["system_name"] == "Jita"
