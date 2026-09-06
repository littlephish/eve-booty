"""Sync writing a structure's changes down as it notices them.

Three code paths feed the history, because three different things upstream
throw the previous value away:

  _corp_structures    INSERT OR REPLACE over the whole row
  _mark_unanchored    learns about an unanchor from an absence, not a value
  _corp_extractions   DELETE and reinsert the whole corp's drill cycles

Each has to be asked what it is about to destroy before it destroys it.
"""

from __future__ import annotations

import json

import pytest

from evasset import db

SEED = """
INSERT INTO sde_regions VALUES (10000002,'The Forge');
INSERT INTO sde_systems VALUES (30000142,'Jita',20000020,10000002,0.9);
INSERT INTO sde_categories VALUES (65,'Structure',1);
INSERT INTO sde_groups VALUES (1657,65,'Citadel',1);
INSERT INTO sde_types (type_id,name,group_id,volume,portion_size,published)
  VALUES (35832,'Astrahus',1657,1,1,1);
"""

CORP = 98000001
SID = 1048466934500
NOW = "2026-09-05T13:42:00Z"
LATER = "2026-09-06T13:42:00Z"


@pytest.fixture
def conn(tmp_path):
    c = db.init(tmp_path / "ch.sqlite")
    c.executescript(SEED)
    return c


class FakeSyncer:
    """The recording halves of Syncer over a real connection, without the ESI
    round trip that normally precedes them."""

    def __init__(self, conn):
        self.conn = conn

    def record(self, incoming, now):
        from evasset.esi.sync import Syncer

        Syncer._record_structure_changes(self, incoming, now)

    def mark(self, corp_id, seen, now=NOW):
        from evasset.esi.sync import Syncer

        Syncer._mark_unanchored(self, corp_id, seen)

    def record_extractions(self, corp_id, incoming, now):
        from evasset.esi.sync import Syncer

        Syncer._record_extraction_changes(self, corp_id, incoming, now)


def esi_structure(**kw):
    row = {
        "structure_id": SID,
        "name": "Ikami - 0064 p11m1",
        "owner_id": CORP,
        "state": "shield_vulnerable",
        "state_timer_end": None,
        "fuel_expires": "2026-09-24T04:00:00Z",
        "reinforce_hour": 18,
        "next_reinforce_hour": None,
        "unanchors_at": None,
        "services": json.dumps([{"name": "Moon Drilling", "state": "online"}]),
    }
    row.update(kw)
    return row


def store(conn, **kw):
    """Put the structure in the table the way a previous sync would have."""
    s = esi_structure(**kw)
    conn.execute(
        """INSERT OR REPLACE INTO structures
             (structure_id,name,system_id,region_id,type_id,owner_id,resolved_at,
              accessible,owned,state,state_timer_end,fuel_expires,reinforce_hour,
              next_reinforce_hour,unanchors_at,services,updated_at)
           VALUES (?,?,30000142,10000002,35832,?, '2026-01-01',1,1,?,?,?,?,?,?,?,?)""",
        (s["structure_id"], s["name"], s["owner_id"], s["state"], s["state_timer_end"],
         s["fuel_expires"], s["reinforce_hour"], s["next_reinforce_hour"],
         s["unanchors_at"], s["services"], "2026-09-01T00:00:00Z"),
    )


def changes(conn, field=None):
    sql = "SELECT * FROM structure_changes"
    args = ()
    if field:
        sql += " WHERE field=?"
        args = (field,)
    return list(conn.execute(sql + " ORDER BY change_id", args))


# ---------------------------------------------------------- first sighting
def test_a_new_structure_is_recorded_as_tracking_started(conn):
    FakeSyncer(conn).record({SID: esi_structure()}, NOW)
    rows = changes(conn)
    assert [(r["field"], r["new_value"]) for r in rows] == [("tracking", "started")]


def test_a_first_sighting_is_one_row_not_one_per_field(conn):
    """Nine watched fields all going from nothing to something would bury the
    real history under a wall of noise on day one."""
    FakeSyncer(conn).record({SID: esi_structure()}, NOW)
    assert len(changes(conn)) == 1


def test_a_first_sighting_does_not_claim_an_anchor(conn):
    """Everything already up when this shipped would otherwise be recorded as
    freshly anchored today, which is simply untrue."""
    FakeSyncer(conn).record({SID: esi_structure()}, NOW)
    assert changes(conn)[0]["field"] != "anchored"


# ------------------------------------------------------------ steady state
def test_a_sync_that_changed_nothing_records_nothing(conn):
    store(conn)
    FakeSyncer(conn).record({SID: esi_structure()}, LATER)
    assert changes(conn) == []


def test_a_state_change_is_recorded_with_both_sides(conn):
    store(conn)
    FakeSyncer(conn).record({SID: esi_structure(state="armor_reinforce")}, LATER)
    row = changes(conn, "state")[0]
    assert row["old_value"] == "shield_vulnerable"
    assert row["new_value"] == "armor_reinforce"


def test_a_change_is_stamped_with_when_the_sync_saw_it(conn):
    store(conn)
    FakeSyncer(conn).record({SID: esi_structure(state="armor_reinforce")}, LATER)
    assert changes(conn, "state")[0]["observed_at"] == LATER


def test_a_reinforcement_records_the_state_and_the_timer(conn):
    store(conn)
    FakeSyncer(conn).record(
        {SID: esi_structure(state="armor_reinforce",
                            state_timer_end="2026-09-26T18:00:00Z")},
        LATER,
    )
    assert {r["field"] for r in changes(conn)} == {"state", "state_timer_end"}


def test_a_refuel_records_both_expiry_times(conn):
    store(conn)
    FakeSyncer(conn).record(
        {SID: esi_structure(fuel_expires="2026-11-02T04:00:00Z")}, LATER
    )
    row = changes(conn, "fuel_expires")[0]
    assert row["old_value"] == "2026-09-24T04:00:00Z"
    assert row["new_value"] == "2026-11-02T04:00:00Z"


def test_reshuffled_services_record_nothing(conn):
    """The trap the differ exists to avoid, pinned end to end."""
    store(conn, services=json.dumps([
        {"name": "Moon Drilling", "state": "online"},
        {"name": "Reprocessing", "state": "online"},
    ]))
    FakeSyncer(conn).record(
        {SID: esi_structure(services=json.dumps([
            {"name": "Reprocessing", "state": "online"},
            {"name": "Moon Drilling", "state": "online"},
        ]))},
        LATER,
    )
    assert changes(conn) == []


# -------------------------------------------------------------- unanchoring
def test_an_unanchor_is_recorded(conn):
    store(conn)
    FakeSyncer(conn).mark(CORP, [999])  # SID no longer reported
    assert changes(conn, "gone_at")


def test_an_unanchor_records_when_it_was_noticed_not_a_value(conn):
    """ESI never says a structure unanchored; it stops mentioning it. The only
    honest thing to record is that we noticed."""
    store(conn)
    FakeSyncer(conn).mark(CORP, [999])
    row = changes(conn, "gone_at")[0]
    assert row["old_value"] is None
    assert row["new_value"] is not None


def test_a_structure_still_reported_is_not_recorded_as_gone(conn):
    store(conn)
    FakeSyncer(conn).mark(CORP, [SID])
    assert changes(conn, "gone_at") == []


def test_an_empty_response_records_nothing(conn):
    """_mark_unanchored already refuses to flag a whole corp on an empty
    page; it must not write history for it either."""
    store(conn)
    FakeSyncer(conn).mark(CORP, [])
    assert changes(conn) == []


def test_a_structure_coming_back_is_recorded(conn):
    """A cancelled unanchor, or a bad sync. The upsert clears gone_at for
    free, so without this the log would show a departure and no return."""
    store(conn)
    conn.execute("UPDATE structures SET gone_at=? WHERE structure_id=?", (NOW, SID))
    FakeSyncer(conn).record({SID: esi_structure()}, LATER)
    row = changes(conn, "gone_at")[0]
    assert row["old_value"] == NOW
    assert row["new_value"] is None


# --------------------------------------------------------- moon extractions
def extraction(conn, start="2026-09-16T18:01:00Z"):
    conn.execute(
        """INSERT OR REPLACE INTO moon_extractions
             (structure_id,moon_id,owner_id,extraction_start_time,
              chunk_arrival_time,natural_decay_time,updated_at)
           VALUES (?,40000001,?,?,'2026-09-16T18:01:00Z','2026-09-16T21:01:00Z',?)""",
        (SID, CORP, start, NOW),
    )


def test_a_new_extraction_is_recorded(conn):
    FakeSyncer(conn).record_extractions(
        CORP, [{"structure_id": SID, "extraction_start_time": "2026-09-16T18:01:00Z"}], NOW
    )
    row = changes(conn, "extraction_start_time")[0]
    assert row["new_value"] == "2026-09-16T18:01:00Z"


def test_the_same_extraction_twice_is_recorded_once(conn):
    """ESI reports the current cycle on every sync. Only its arrival is news."""
    extraction(conn)
    FakeSyncer(conn).record_extractions(
        CORP, [{"structure_id": SID, "extraction_start_time": "2026-09-16T18:01:00Z"}], LATER
    )
    assert changes(conn, "extraction_start_time") == []


def test_a_new_cycle_is_recorded(conn):
    extraction(conn)
    FakeSyncer(conn).record_extractions(
        CORP, [{"structure_id": SID, "extraction_start_time": "2026-10-16T18:01:00Z"}], LATER
    )
    row = changes(conn, "extraction_start_time")[0]
    assert row["old_value"] == "2026-09-16T18:01:00Z"
    assert row["new_value"] == "2026-10-16T18:01:00Z"


def test_an_extraction_ending_is_recorded(conn):
    """The drill stops being reported once the cycle is done."""
    extraction(conn)
    FakeSyncer(conn).record_extractions(CORP, [], LATER)
    row = changes(conn, "extraction_start_time")[0]
    assert row["old_value"] == "2026-09-16T18:01:00Z"
    assert row["new_value"] is None
