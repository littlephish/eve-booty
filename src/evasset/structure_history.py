"""What changed about a structure, and how to say it.

Two halves, deliberately kept together because they are two ends of the same
rule and drift apart if they are not:

  diff()         decides what counts as a change, for the sync to record
  group_events() decides what a recorded change is called, for the tab to show

Both are Qt-free and database-free, the same way fitting.py and pricing.py
are, so the judgement calls in here can be tested one sentence at a time.

The split between them is the point of the whole design. What sync stores is
the raw before-and-after of a field -- durable fact, no opinion. What the tab
shows is a sentence, and sentences are opinions that get revised. Keeping the
opinion at read time means changing it re-renders every row already recorded;
storing finished sentences would freeze today's wording into the database.

An important limitation, recorded here because it is invisible from the
outside: history starts when the feature was installed. ESI has no structure
event log, and both writers upstream of this overwrote the previous value on
every sync, so nothing before the first sync can ever be recovered. A
structure's first sighting is therefore "tracking started", not "anchored" --
claiming an anchor date for a structure that has been up for a year would be
a fabrication, and one that could not even be softened, because every
timestamp here means "a sync noticed", not "this happened in game".
"""

from __future__ import annotations

import json

# Compared between syncs. Deliberately not everything on the row: updated_at
# and resolved_at are rewritten by every sync whether or not anything about
# the structure moved, so watching them would put one meaningless row per
# structure per sync in the log and drown the changes worth reading.
WATCHED_FIELDS = (
    "name",
    "owner_id",
    "state",
    "state_timer_end",
    "fuel_expires",
    "reinforce_hour",
    "next_reinforce_hour",
    "unanchors_at",
    "services",
)

# Fields recorded by the code paths that bypass the ordinary field-by-field
# comparison, kept here so the read side has one list of everything it might
# meet. "tracking" is the single row written the first time a structure is
# seen; "gone_at" comes from _mark_unanchored, which learns about an unanchor
# from a structure's absence rather than from any value changing.
TRACKING = "tracking"
GONE = "gone_at"
EXTRACTION = "extraction_start_time"


def _services_key(raw):
    """A canonical form of the services blob, for comparison only.

    ESI returns services as a JSON array and promises nothing about its
    order. Comparing the raw text would manufacture a change the first sync
    that came back shuffled, and another one the sync after that when it
    shuffled back -- an infinite supply of history about nothing.

    Anything unparseable falls back to the raw string rather than raising:
    this runs inside a sync that is writing everything else at the same time,
    and a malformed blob is worth one wrong history row, not a failed sync.
    """
    try:
        services = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return ("unparseable", str(raw))
    if not isinstance(services, list):
        return ("unparseable", str(raw))
    return tuple(sorted(
        (str(s.get("name", "")), str(s.get("state", "")))
        for s in services
        if isinstance(s, dict)
    ))


def _compare_key(field: str, value):
    """What equality means for one field.

    Everything that is not services is compared as text. The two sides come
    from different places -- one out of SQLite, one out of ESI's JSON -- and
    an integer that made that round trip must not read as a change just
    because it arrived as 18 one way and "18" the other.
    """
    if field == "services":
        return _services_key(value)
    return None if value is None else str(value)


def _stored(value):
    """The form a value is written in: raw, as text, or NULL."""
    return None if value is None else str(value)


def diff(old, new) -> list[tuple[str, str | None, str | None]]:
    """[(field, old_value, new_value), ...] for everything watched that moved.

    Both arguments are mappings -- a sqlite3.Row of what is stored and a dict
    of what just arrived. Values come back in the form they should be
    *stored* in, not the form they were compared in, so the recorded row
    keeps ESI's own text and the History tab can render it however it likes.
    """
    changes = []
    for field in WATCHED_FIELDS:
        before = old[field] if field in old.keys() else None
        after = new[field] if field in new.keys() else None
        if _compare_key(field, before) != _compare_key(field, after):
            changes.append((field, _stored(before), _stored(after)))
    return changes
