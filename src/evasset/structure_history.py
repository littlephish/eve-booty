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
from dataclasses import dataclass

from .evetime import fmt_eve, parse_utc

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


# --------------------------------------------------------------- the wording
# Everything below turns recorded changes into sentences. It is the revisable
# half: none of these strings are stored, so changing one re-words history
# that has already been collected rather than leaving old rows reading the
# old way and new rows the new way.
@dataclass
class Event:
    """One line of a structure's history.

    `at` is when a sync noticed, never when it happened in game -- the tab
    says so once at the top rather than hedging every row. `kind` is the field
    the line came from, offered so a view can colour a reinforcement
    differently from a rename without re-parsing the text.
    """

    at: str
    kind: str
    text: str


def _when(value) -> str:
    """An ESI timestamp as EVE time, or the raw value if it will not parse."""
    parsed = parse_utc(value)
    return fmt_eve(parsed) if parsed else str(value or "")


def _label(state) -> str:
    return str(state or "").replace("_", " ").capitalize()


def _hour(value) -> str:
    try:
        return f"{int(value):02d}:00"
    except (TypeError, ValueError):
        return str(value or "?")


def _service_map(raw) -> dict[str, str]:
    try:
        services = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return {}
    if not isinstance(services, list):
        return {}
    return {
        str(s.get("name", "?")): str(s.get("state", "?"))
        for s in services
        if isinstance(s, dict)
    }


def _services_text(old, new) -> str:
    """Name what actually moved, rather than saying "services changed".

    Three things can happen to a service and they read very differently: one
    went offline (something is wrong), one was fitted (somebody worked on
    it), one was removed. Naming them is the whole value of the line.
    """
    before, after = _service_map(old), _service_map(new)
    parts = []
    for name, state in sorted(after.items()):
        if name not in before:
            parts.append(f"{name} fitted")
        elif before[name] != state:
            parts.append(f"{name} went {state}")
    parts += [f"{name} removed" for name in sorted(before) if name not in after]
    return "Services: " + ", ".join(parts) if parts else "Services changed"


def _fuel_text(old, new) -> str:
    """A forward jump is always called a refuel.

    Known to be wrong in one case, and accepted deliberately: fuel_expires is
    CCP's arithmetic on fuel divided by burn rate, so it also jumps forward
    when a service goes offline and the structure starts burning less. That
    reads as "Refuelled" here when nobody refuelled anything.

    The alternative -- only claiming a refuel when the service list held still
    in the same sync -- was considered and turned down in favour of the line
    always reading the same way. Both times are printed, which is what lets a
    reader tell a two-hour drift from a forty-day haul even though the label
    does not.
    """
    before, after = parse_utc(old), parse_utc(new)
    if before and after and after > before:
        return f"Refuelled — fuel now lasts until {_when(new)} (was {_when(old)})"
    if new and not old:
        return f"Fuel clock starts, lasts until {_when(new)}"
    if old and not new:
        return "Fuel clock cleared"
    return f"Fuel now runs out {_when(new)} (was {_when(old)})"


def _sentence(field: str, old, new, group: dict) -> str:
    if field == TRACKING:
        return (
            "Tracking started — this structure was already here when "
            "EVE Booty first looked"
        )
    if field == GONE:
        if new:
            return "Unanchored, or ESI stopped reporting it"
        return "Reported again after going missing"
    if field == "state":
        text = f"{_label(old)} → {_label(new)}"
        # The timer is the part anybody opens this for, and it arrives as its
        # own row from the same sync.
        timer = group.get("state_timer_end")
        if timer and timer[1]:
            text += f", timer ends {_when(timer[1])}"
        return text
    if field == "state_timer_end":
        return f"Timer ends {_when(new)}" if new else "Timer cleared"
    if field == "fuel_expires":
        return _fuel_text(old, new)
    if field == "services":
        return _services_text(old, new)
    if field == "unanchors_at":
        return f"Unanchor started, completes {_when(new)}" if new else "Unanchor cancelled"
    if field == EXTRACTION:
        return f"Moon extraction started, chunk due {_when(new)}" if new else (
            "Moon extraction ended"
        )
    if field == "name":
        return f"Renamed from {old} to {new}"
    if field == "owner_id":
        return f"Owner changed from {old} to {new}"
    if field in ("reinforce_hour", "next_reinforce_hour"):
        return f"Vulnerability window moved to {_hour(new)} (was {_hour(old)})"
    # A field added to WATCHED_FIELDS without a sentence to go with it. Shown
    # plainly rather than dropped: a change nobody worded is still a change,
    # and silently discarding it would hide the very thing somebody just
    # started recording.
    return f"{field}: {old} → {new}"


# Changes that are part of another line rather than a line of their own.
# state_timer_end is folded into the state sentence when both arrive from the
# same sync, and would otherwise say the same thing twice.
_ABSORBED = {"state_timer_end": "state"}


def group_events(rows) -> list[Event]:
    """Recorded changes -> the History tab's lines, newest sync first.

    Grouped by observed_at because one sync is one thing that happened: a
    reinforcement writes both `state` and `state_timer_end`, and reading them
    as two separate entries would split one event across two lines and put
    the timer -- the part that matters -- on the line without the context.
    """
    by_sync: dict[str, dict[str, tuple]] = {}
    for row in rows:
        at = row["observed_at"]
        by_sync.setdefault(at, {})[row["field"]] = (
            row["old_value"], row["new_value"]
        )

    events = []
    for at in sorted(by_sync, reverse=True):
        group = by_sync[at]
        for field, (old, new) in sorted(group.items()):
            if _ABSORBED.get(field) in group:
                continue
            events.append(Event(at=at, kind=field, text=_sentence(field, old, new, group)))
    return events
