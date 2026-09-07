"""The saved-view library: named whole views of the Assets tab.

A view is the working posture of the tab -- the filter as `omni.to_text()`
grammar, the group-by key, the rail level and the rail's sort -- kept under
a name so it can be listed, renamed, recalled from a digit slot and shared
as one line of grammar. It replaced nine anonymous `saved_views(slot,
state_json)` rows at schema v7: a slot was a filter nobody could name, so
nothing listed the views, freed a slot or told two of them apart, and "the
ships short of paste" view -- a question a pilot asks every week -- was worth
keeping properly.

The columns are the payload, not a JSON blob. `state_json` bought the old
table the freedom to grow without a migration and cost it every query worth
having: the Load card wants to show the filter line beside the name, which a
blob cannot be asked for. The cost is that a new column needs a migration,
and this schema has had one: `rail_sort` arrived at v8 as a rebuild-and-copy.

Qt-free like `fits.py` and `stockpile.py`, and for the same reason: the
writes live here beside their reads rather than half in `queries.py`, so the
one module that owns the `views` table can be read in one sitting and the
whole library is exercised by plain pytest without a window.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import omni

# The digits `Ctrl+n` loads from. A view is given its digit from the key cell
# on the Save card or the Load card's row, never by a keystroke: Ctrl+n used
# to save into the slot, and one keystroke could overwrite a view. Nine
# because that is how many digit keys are free on the tab; the CHECK
# constraint in the schema carries the same range.
SLOTS = tuple(range(1, 10))

# The name a view with no filter at all is offered, and the one a slot save
# invents when the slot is empty.
UNFILTERED = "Unfiltered"
SLOT_NAME = "Slot {slot}"

# How many chip labels a suggested name carries, and how long the whole
# suggestion may get before it is cut. Three parts and forty characters is
# what fits the Save card's name field and the Load card's rows without
# wrapping; a name is a handle, not a description of the filter, which the
# row shows underneath anyway.
_NAME_PARTS = 3
_NAME_LIMIT = 40

# The SELECT every reader shares. It ends after FROM with a newline so a
# caller appends its own " WHERE ..." or " ORDER BY ..." clause; the leading
# space on those fragments is what keeps them from gluing onto "views".
_COLUMNS = """
SELECT view_id, name, slot, filter_text, group_by, rail_level, rail_sort,
       created_at, updated_at
FROM views
"""


def _now() -> str:
    """ISO 8601 UTC to the second, the `fits.py` timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ViewState:
    """The remembered parts of a view: filter line, group-by, rail level and sort.

    `rail_sort` is the key as the rail names it ("value", "name", "volume" --
    `ui/rail.py`'s SORT_KEYS), and an empty string means "not recorded":
    a view saved before v8 or imported from a shared line leaves the rail's
    sort alone when it is loaded, the same rule an empty `rail_level` has
    always followed.
    """

    filter: str = ""
    group_by: str = ""
    rail_level: str = ""
    rail_sort: str = ""


@dataclass
class View:
    """One row of the library, with its state gathered into a `ViewState`."""

    view_id: int
    name: str
    slot: int | None
    state: ViewState = field(default_factory=ViewState)
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> View:
        return cls(
            view_id=int(row["view_id"]),
            name=row["name"],
            slot=None if row["slot"] is None else int(row["slot"]),
            state=ViewState(
                filter=row["filter_text"],
                group_by=row["group_by"],
                rail_level=row["rail_level"],
                rail_sort=row["rail_sort"],
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class NameTaken(ValueError):
    """A rename asked for a name another view already holds.

    Raised instead of letting the UNIQUE index throw an IntegrityError,
    because the card has to say which name is the problem and an
    IntegrityError's message is a sentence about an index.
    """

    def __init__(self, name: str):
        super().__init__(f"a view named {name!r} already exists")
        self.name = name


# ------------------------------------------------------------------- reads
def list_views(conn: sqlite3.Connection) -> list[View]:
    """Every saved view: the slotted ones in digit order, then the rest by name.

    Slots first because the digit keys are the fast path and a pilot reading
    the list is usually checking which digit holds what; `slot IS NULL` sorts
    the unslotted views after them rather than before, SQLite putting NULLs
    first on its own.
    """
    rows = conn.execute(_COLUMNS + " ORDER BY slot IS NULL, slot, name COLLATE NOCASE")
    return [View.from_row(r) for r in rows]


def get_view(conn: sqlite3.Connection, view_id: int) -> View | None:
    row = conn.execute(_COLUMNS + " WHERE view_id = ?", (int(view_id),)).fetchone()
    return View.from_row(row) if row is not None else None


def find_view(conn: sqlite3.Connection, name: str) -> View | None:
    """The view of that name, case-insensitively and ignoring surrounding space.

    `save:jita ships` and a stored "Jita Ships" are the same view: the column
    collates NOCASE so the lookup, the UNIQUE index and the omnibox's own
    case-folding all agree, and a name typed with a stray trailing space is a
    typo rather than a second view.
    """
    row = conn.execute(
        _COLUMNS + " WHERE name = ? COLLATE NOCASE", ((name or "").strip(),)
    ).fetchone()
    return View.from_row(row) if row is not None else None


def view_in_slot(conn: sqlite3.Connection, slot: int) -> View | None:
    row = conn.execute(_COLUMNS + " WHERE slot = ?", (int(slot),)).fetchone()
    return View.from_row(row) if row is not None else None


def _reload(conn: sqlite3.Connection, view_id: int) -> View:
    """Re-read a row this module has just written, in the caller's transaction.

    The row cannot be missing, so a miss is a bug here rather than a case
    every caller must branch on; raising keeps the return type honest.
    """
    view = get_view(conn, view_id)
    if view is None:
        raise KeyError(f"view {view_id} vanished mid-transaction")
    return view


# ------------------------------------------------------------------ writes
def save_view(conn: sqlite3.Connection, name: str, state: ViewState) -> tuple[View, bool]:
    """Store `state` under `name`, replacing any view already called that.

    Returns the stored view and whether it replaced one. Saving over an
    existing name is an edit rather than an error -- the same decision
    `fits.save_fit` made for a revised rack -- so the upsert deliberately
    touches only the state and `updated_at`: the row keeps its `view_id` (so
    a card's selection survives), its `slot` (so re-saving the view on digit
    3 does not knock it off digit 3) and its `created_at` and stored spelling
    of the name (so "Jita Ships" does not silently become "jita ships"
    because that is how it was typed the second time).

    `replaced` is read before the write rather than from the upsert, which
    cannot report which branch it took; the caller wraps both in one
    transaction, so nothing can slip in between.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("a view needs a name")
    replaced = find_view(conn, name) is not None
    now = _now()
    conn.execute(
        """INSERT INTO views (name, slot, filter_text, group_by, rail_level, rail_sort,
                              created_at, updated_at)
           VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET filter_text = excluded.filter_text,
                                           group_by    = excluded.group_by,
                                           rail_level  = excluded.rail_level,
                                           rail_sort   = excluded.rail_sort,
                                           updated_at  = excluded.updated_at""",
        (name, state.filter, state.group_by, state.rail_level, state.rail_sort, now, now),
    )
    stored = find_view(conn, name)
    if stored is None:
        # The upsert wrote this row a statement ago, inside the caller's
        # transaction. A miss is a bug in this module, not a case a caller
        # could sensibly handle, so it raises rather than returning None and
        # pushing the impossible branch into every UI handler.
        raise KeyError(f"the view named {name!r} vanished mid-transaction")
    return stored, replaced


def update_state(conn: sqlite3.Connection, view_id: int, state: ViewState) -> None:
    """Re-point an existing view at the current posture, name and slot intact."""
    conn.execute(
        """UPDATE views SET filter_text = ?, group_by = ?, rail_level = ?, rail_sort = ?,
                            updated_at = ?
           WHERE view_id = ?""",
        (
            state.filter, state.group_by, state.rail_level, state.rail_sort,
            _now(), int(view_id),
        ),
    )


def rename_view(conn: sqlite3.Connection, view_id: int, name: str) -> None:
    """Give one view a new name, or the same name in a different case.

    The clash is checked explicitly rather than left to the UNIQUE index so
    the card meets a `NameTaken` carrying the offending name instead of an
    IntegrityError. Renaming a view to its own name in another case is a
    correction, not a clash, and updates the stored spelling.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("a view needs a name")
    other = find_view(conn, name)
    if other is not None and other.view_id != int(view_id):
        raise NameTaken(name)
    conn.execute(
        "UPDATE views SET name = ?, updated_at = ? WHERE view_id = ?",
        (name, _now(), int(view_id)),
    )


def delete_view(conn: sqlite3.Connection, view_id: int) -> None:
    """Forget one view. Its slot, if it held one, is freed with the row."""
    conn.execute("DELETE FROM views WHERE view_id = ?", (int(view_id),))


def set_slot(conn: sqlite3.Connection, view_id: int, slot: int | None) -> None:
    """Put a view on a digit, or take it off one.

    A digit holds at most one view, so the previous holder is cleared first;
    call this inside `db.transaction` or a crash between the two statements
    leaves the digit empty. The UNIQUE constraint on `slot` is the backstop
    rather than the mechanism -- it turns a second writer racing this pair
    into an IntegrityError instead of two views quietly claiming one key.
    """
    if slot is not None and slot not in SLOTS:
        raise ValueError(f"slot must be one of {SLOTS} or None, not {slot!r}")
    if slot is not None:
        conn.execute(
            "UPDATE views SET slot = NULL WHERE slot = ? AND view_id <> ?",
            (int(slot), int(view_id)),
        )
    conn.execute(
        "UPDATE views SET slot = ? WHERE view_id = ?",
        (None if slot is None else int(slot), int(view_id)),
    )


def save_to_slot(conn: sqlite3.Connection, slot: int, state: ViewState) -> tuple[View, bool]:
    """Write the current posture into the view on one digit.

    No keystroke reaches this any more -- `Ctrl+n` loads (see SLOTS) and a
    digit is handed out from the key cell -- so it is the digit-addressed
    save for a caller that knows the slot rather than the name, which is
    how the integration tests put a view on a digit. Returns the view and
    whether a new one was created. A digit that already holds a view is
    updated in place, keeping the name the user gave it: "make digit 3
    this", not "rename digit 3". An empty digit invents `Slot n`, which may
    itself land on an unslotted view already called that (a view whose slot
    was taken away); adopting it is better than raising where nothing can
    ask a question.
    """
    if slot not in SLOTS:
        raise ValueError(f"slot must be one of {SLOTS}, not {slot!r}")
    holder = view_in_slot(conn, slot)
    if holder is not None:
        update_state(conn, holder.view_id, state)
        return _reload(conn, holder.view_id), False
    view, replaced = save_view(conn, SLOT_NAME.format(slot=slot), state)
    set_slot(conn, view.view_id, slot)
    return _reload(conn, view.view_id), not replaced


def import_text(conn: sqlite3.Connection, text: str, name: str) -> tuple[View, bool]:
    """Add a shared filter line to the library as a view.

    The line is canonicalised through `omni.parse` and `to_text` rather than
    stored as typed, so a hand-written `location:"Jita IV - Moon 4"` is kept
    the way the omnibox itself would write it and two people pasting the same
    filter in different spellings get the same stored line. Group-by, rail
    level and rail sort stay empty: only the filter half of a view travels as
    one line today, and an imported view therefore leaves all three controls
    alone when it is loaded.

    A blank line, or one that parses to no filter at all (a lone unbalanced
    quote does), is nothing to import rather than a view meaning
    "everything".
    """
    spec = omni.parse(text or "")
    if spec.is_empty:
        raise ValueError("nothing to import")
    return save_view(conn, (name or "").strip() or suggest_name(spec), ViewState(spec.to_text()))


# ------------------------------------------------------------------ naming
def suggest_name(spec: omni.FilterSpec) -> str:
    """A short human label for a filter, offered as a view's name.

    Reads as the grammar does -- `cat Ship · loc Jita` -- because the person
    naming the view just typed those chips and a label spelled any other way
    is one more thing to translate. Pure, taking a spec rather than a
    connection, so the Load card can suggest a name for a line pasted into
    its import box before anything is stored.
    """
    parts: list[str] = []
    for chip in spec.chips:
        sign = "-" if chip.negated else ""
        if chip.kind == omni.ABYSSAL_KIND and not chip.value:
            parts.append(sign + omni.ABYSSAL_KIND)
        else:
            parts.append(f"{sign}{omni.prefix_for_kind(chip.kind)} {chip.value}")
    if spec.text.strip():
        parts.append(spec.text.strip())
    if not parts:
        return UNFILTERED
    label = " · ".join(parts[:_NAME_PARTS])
    if len(label) > _NAME_LIMIT:
        label = label[: _NAME_LIMIT - 1].rstrip() + "…"
    return label


# --------------------------------------------------------------------- diff
def diff_tokens(current_line: str, view_line: str) -> list[tuple[str, omni.Chip | str]]:
    """What loading `view_line` over `current_line` would change, token by token.

    Returns `(sign, token)` pairs: "-" for a token the load drops (in the
    current filter, not in the view), "=" for one both share and "+" for one
    the view brings in, in that order -- drops first because they are the
    cost of the load, which is what the Load card exists to show before the
    filter is thrown away. A token is an `omni.Chip`, matched exactly on kind,
    value and negation (`-owner:Main` and `owner:Main` are two tokens, not one
    changed), or a bare search word matched as itself; the two never compare
    equal. Duplicates within one line collapse, as they do in the filter.

    Pure and Qt-free so the comparison the card renders is the one pinned in
    tests/test_views.py rather than a rule buried in a paint routine.
    """

    def tokens(line: str) -> list:
        spec = omni.parse(line or "")
        seen: list = []
        for token in [*spec.chips, *spec.text.split()]:
            if token not in seen:
                seen.append(token)
        return seen

    current, wanted = tokens(current_line), tokens(view_line)
    dropped = [("-", token) for token in current if token not in wanted]
    shared = [("=", token) for token in current if token in wanted]
    added = [("+", token) for token in wanted if token not in current]
    return dropped + shared + added
