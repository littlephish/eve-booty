"""Deciding what changed about a structure between two syncs.

Pure comparison, no database and no Qt, so the rules can be pinned one at a
time. The write side (sync) and the wording side (the History tab) both build
on this and neither has an opinion about it.

The rule that matters most here is the one about `services`: it is a JSON
array, ESI does not promise its order, and comparing the raw text would
manufacture a "services changed" row on the first sync after CCP happened to
serialise it differently. Every field gets normalised before comparison and
stored raw afterwards.
"""

from __future__ import annotations

import json

from evasset import structure_history as sh


def svc(*pairs):
    return json.dumps([{"name": n, "state": s} for n, s in pairs])


def structure(**kw):
    row = {
        "name": "Ikami - 0064 p11m1",
        "owner_id": 98000001,
        "state": "shield_vulnerable",
        "state_timer_end": None,
        "fuel_expires": "2026-09-24T04:00:00Z",
        "reinforce_hour": 18,
        "next_reinforce_hour": None,
        "unanchors_at": None,
        "services": svc(("Moon Drilling", "online")),
    }
    row.update(kw)
    return row


def fields(changes):
    return [field for field, _, _ in changes]


# ------------------------------------------------------------- no news
def test_an_unchanged_structure_produces_nothing():
    assert sh.diff(structure(), structure()) == []


def test_a_field_going_from_none_to_none_is_not_a_change():
    assert sh.diff(structure(unanchors_at=None), structure(unanchors_at=None)) == []


def test_an_int_that_survived_a_round_trip_is_not_a_change():
    """Stored values come back out of SQLite; incoming ones come from ESI's
    JSON. 18 and 18 must compare equal however they got here."""
    assert sh.diff(structure(reinforce_hour=18), structure(reinforce_hour="18")) == []


# ------------------------------------------------------------- real changes
def test_a_state_change_is_recorded():
    changes = sh.diff(structure(), structure(state="armor_reinforce"))
    assert fields(changes) == ["state"]


def test_both_sides_of_the_change_are_kept():
    """The History tab words a fuel jump using both, and "was Y" is the only
    thing that distinguishes a two-hour nudge from a forty-day haul."""
    changes = sh.diff(structure(), structure(fuel_expires="2026-11-02T04:00:00Z"))
    assert changes == [
        ("fuel_expires", "2026-09-24T04:00:00Z", "2026-11-02T04:00:00Z")
    ]


def test_two_fields_changing_at_once_give_two_changes():
    """A reinforcement moves the state and the timer together. They are
    recorded separately and rejoined at read time."""
    changes = sh.diff(
        structure(),
        structure(state="armor_reinforce", state_timer_end="2026-09-26T18:00:00Z"),
    )
    assert sorted(fields(changes)) == ["state", "state_timer_end"]


def test_a_value_appearing_is_a_change():
    changes = sh.diff(structure(), structure(unanchors_at="2026-10-01T12:00:00Z"))
    assert changes == [("unanchors_at", None, "2026-10-01T12:00:00Z")]


def test_a_value_disappearing_is_a_change():
    """An unanchor being cancelled, which is worth a line in the log."""
    changes = sh.diff(structure(unanchors_at="2026-10-01T12:00:00Z"), structure())
    assert changes == [("unanchors_at", "2026-10-01T12:00:00Z", None)]


# ------------------------------------------------------------- the services trap
def test_reordering_the_services_is_not_a_change():
    """ESI does not promise array order. Comparing raw JSON would log a
    change on every sync that happened to come back shuffled -- and then the
    next one would log it right back."""
    before = structure(services=svc(("Moon Drilling", "online"), ("Reprocessing", "online")))
    after = structure(services=svc(("Reprocessing", "online"), ("Moon Drilling", "online")))
    assert sh.diff(before, after) == []


def test_a_service_going_offline_is_a_change():
    before = structure(services=svc(("Moon Drilling", "online")))
    after = structure(services=svc(("Moon Drilling", "offline")))
    assert fields(sh.diff(before, after)) == ["services"]


def test_a_service_being_added_is_a_change():
    before = structure(services=svc(("Moon Drilling", "online")))
    after = structure(services=svc(("Moon Drilling", "online"), ("Market Hub", "online")))
    assert fields(sh.diff(before, after)) == ["services"]


def test_unparseable_services_do_not_blow_up_the_sync():
    """Whatever else happens, a malformed blob must not stop the differ --
    the sync that calls this is writing everything else at the same time."""
    before = structure(services="not json")
    after = structure(services=svc(("Moon Drilling", "online")))
    assert fields(sh.diff(before, after)) == ["services"]


# ------------------------------------------------------------- scope
def test_only_the_watched_fields_are_compared():
    """updated_at changes on literally every sync. Watching it would mean a
    history row per sync per structure, and no signal at all."""
    assert "updated_at" not in sh.WATCHED_FIELDS
    assert "resolved_at" not in sh.WATCHED_FIELDS


def test_an_unwatched_field_changing_is_ignored():
    before = dict(structure(), updated_at="2026-09-01T00:00:00Z")
    after = dict(structure(), updated_at="2026-09-05T00:00:00Z")
    assert sh.diff(before, after) == []
