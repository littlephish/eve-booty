"""Turning recorded field changes into sentences.

The read half of the design: sync stores "fuel_expires went from A to B", and
this decides that reads as "Refuelled". Keeping the decision here rather than
in the database is what lets it be revised later without losing history --
change a rule and every row already recorded re-renders under it.

Pure functions over plain dicts, so each sentence can be pinned on its own.
"""

from __future__ import annotations

import json

from evasset import structure_history as sh

AT = "2026-09-05T13:42:00Z"
EARLIER = "2026-09-01T09:00:00Z"


def change(field, old=None, new=None, at=AT):
    return {"observed_at": at, "field": field, "old_value": old, "new_value": new}


def texts(rows):
    return [event.text for event in sh.group_events(rows)]


def svc(*pairs):
    return json.dumps([{"name": n, "state": s} for n, s in pairs])


# ------------------------------------------------------------ first sighting
def test_a_first_sighting_says_tracking_not_anchored():
    """Every structure already up when this shipped hits this line. Calling it
    an anchor would date them all to the day of the upgrade."""
    text = texts([change("tracking", new="started")])[0]
    assert "racking" in text
    assert "nchored" not in text


# -------------------------------------------------------------- reinforcement
def test_a_state_change_reads_as_one_state_to_another():
    text = texts([change("state", "shield_vulnerable", "armor_reinforce")])[0]
    assert "Shield vulnerable" in text
    assert "Armor reinforce" in text


def test_a_reinforcement_and_its_timer_are_one_sentence():
    """They are two rows because they are two fields, but they are one event
    and reading them as two would be worse than useless -- the timer is the
    part anybody cares about."""
    events = sh.group_events([
        change("state", "shield_vulnerable", "armor_reinforce"),
        change("state_timer_end", None, "2026-09-26T18:00:00Z"),
    ])
    assert len(events) == 1
    assert "2026-09-26 18:00" in events[0].text


def test_a_timer_moving_on_its_own_still_reads():
    text = texts([change("state_timer_end", None, "2026-09-26T18:00:00Z")])[0]
    assert "2026-09-26 18:00" in text


# ---------------------------------------------------------------------- fuel
def test_fuel_moving_forward_reads_as_a_refuel():
    text = texts([
        change("fuel_expires", "2026-09-24T04:00:00Z", "2026-11-02T04:00:00Z")
    ])[0]
    assert "Refuelled" in text


def test_a_refuel_shows_both_times():
    """The label is the same for a two-hour nudge and a forty-day haul -- a
    forward jump is always called a refuel -- so the numbers are what tells
    them apart, and they have to be on the line."""
    text = texts([
        change("fuel_expires", "2026-09-24T04:00:00Z", "2026-11-02T04:00:00Z")
    ])[0]
    assert "2026-11-02 04:00" in text
    assert "2026-09-24 04:00" in text


def test_fuel_moving_backwards_is_not_called_a_refuel():
    """Fuel pulled out, or a new service raising the burn rate. Whatever it
    was, nobody refuelled anything."""
    text = texts([
        change("fuel_expires", "2026-11-02T04:00:00Z", "2026-09-24T04:00:00Z")
    ])[0]
    assert "Refuelled" not in text


def test_fuel_appearing_from_nothing_is_not_called_a_refuel():
    """No previous expiry means no comparison to make."""
    text = texts([change("fuel_expires", None, "2026-11-02T04:00:00Z")])[0]
    assert "Refuelled" not in text
    assert "2026-11-02 04:00" in text


# ------------------------------------------------------------------ services
def test_a_service_going_offline_is_named():
    text = texts([change("services",
                         svc(("Moon Drilling", "online")),
                         svc(("Moon Drilling", "offline")))])[0]
    assert "Moon Drilling" in text
    assert "offline" in text


def test_a_service_being_fitted_is_named():
    text = texts([change("services",
                         svc(("Moon Drilling", "online")),
                         svc(("Moon Drilling", "online"), ("Market Hub", "online")))])[0]
    assert "Market Hub" in text


def test_a_service_being_removed_is_named():
    text = texts([change("services",
                         svc(("Moon Drilling", "online"), ("Market Hub", "online")),
                         svc(("Moon Drilling", "online")))])[0]
    assert "Market Hub" in text


def test_unreadable_services_do_not_produce_a_crash():
    assert texts([change("services", "not json", svc(("Moon Drilling", "online")))])


# ------------------------------------------------------------- coming and going
def test_a_structure_going_missing_reads_as_an_unanchor():
    text = texts([change("gone_at", None, AT)])[0]
    assert "nanchor" in text or "stopped reporting" in text


def test_a_structure_coming_back_says_so():
    text = texts([change("gone_at", AT, None)])[0]
    assert "again" in text.lower()


def test_an_unanchor_being_scheduled_reads():
    text = texts([change("unanchors_at", None, "2026-10-01T12:00:00Z")])[0]
    assert "2026-10-01 12:00" in text


def test_an_unanchor_being_cancelled_reads():
    text = texts([change("unanchors_at", "2026-10-01T12:00:00Z", None)])[0]
    assert "ancel" in text


# ---------------------------------------------------------------- extractions
def test_an_extraction_starting_reads():
    text = texts([change("extraction_start_time", None, "2026-09-16T18:01:00Z")])[0]
    assert "xtraction" in text


def test_an_extraction_ending_reads():
    text = texts([change("extraction_start_time", "2026-09-16T18:01:00Z", None)])[0]
    assert "xtraction" in text
    assert "ended" in text.lower()


# --------------------------------------------------------------- housekeeping
def test_a_rename_shows_both_names():
    text = texts([change("name", "Old Name", "New Name")])[0]
    assert "Old Name" in text
    assert "New Name" in text


def test_the_vulnerability_window_moving_reads_as_hours():
    text = texts([change("reinforce_hour", "2", "9")])[0]
    assert "09:00" in text
    assert "02:00" in text


def test_an_unknown_field_still_produces_a_line():
    """A field added to WATCHED_FIELDS without a sentence to go with it must
    show up as something rather than vanish from the history."""
    assert texts([change("some_new_field", "a", "b")])


# --------------------------------------------------------------------- order
def test_the_newest_change_comes_first():
    """The tab reads top-down and the interesting end is the recent one."""
    events = sh.group_events([
        change("state", "shield_vulnerable", "armor_reinforce", at=EARLIER),
        change("name", "Old", "New", at=AT),
    ])
    assert [e.at for e in events] == [AT, EARLIER]


def test_changes_from_different_syncs_are_not_merged():
    events = sh.group_events([
        change("state", "a", "b", at=EARLIER),
        change("state", "b", "c", at=AT),
    ])
    assert len(events) == 2


def test_nothing_recorded_is_no_events():
    assert sh.group_events([]) == []
