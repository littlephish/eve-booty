"""Clicking a Structures column header actually reorders the rows.

The bug this pins: SortController deliberately sorts the *source model*
rather than the proxy in front of it (see its module docstring -- proxy
sorting cost 12.6 seconds a click on a big table). RowTableModel implements
sort() and so the Assets and Wallet tabs reorder. _StructuresModel does not,
and QAbstractTableModel.sort() is a base-class no-op, so every header click
on the Structures tab moved the sort arrow and reordered nothing.

That failure mode is worth a test rather than a look: the header highlights,
the little triangle flips, and the only thing missing is the part nobody
screenshots. It reads as "sorting is wired up" right up until you check the
first row.

_StructuresModel.data() already returns a considered Qt.UserRole key per
column -- chronological for the deadline columns, numeric for the vuln hour
-- written for a proxy that was never asked to sort. sort() uses those same
keys, so there is one definition of what a column sorts by.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402

from evasset.ui.structures_view import _StructuresModel  # noqa: E402

COLUMN = {key: i for i, (key, _) in enumerate(_StructuresModel.COLUMNS)}


def structure(name, **kw):
    row = {
        "name": name,
        "type_name": "Astrahus",
        "system_name": "Jita",
        "region_name": "The Forge",
        "state": "shield_vulnerable",
        "state_timer_start": None,
        "state_timer_end": None,
        "fuel_expires": None,
        "reinforce_hour": None,
        "next_reinforce_hour": None,
        "next_reinforce_apply": None,
        "chunk_arrival_time": None,
        "natural_decay_time": None,
        "services": None,
        "gone_at": None,
    }
    row.update(kw)
    return row


def names(model):
    return [
        model.data(model.index(r, COLUMN["name"]), Qt.DisplayRole)
        for r in range(model.rowCount())
    ]


# ------------------------------------------------------------------ the bug
def test_sorting_by_name_reorders_the_rows(qapp_or_skip):
    model = _StructuresModel([structure("Zebra"), structure("Alpha"), structure("Mike")])
    model.sort(COLUMN["name"], Qt.AscendingOrder)
    assert names(model) == ["Alpha", "Mike", "Zebra"]


def test_sorting_descending_reverses_it(qapp_or_skip):
    model = _StructuresModel([structure("Alpha"), structure("Zebra"), structure("Mike")])
    model.sort(COLUMN["name"], Qt.DescendingOrder)
    assert names(model) == ["Zebra", "Mike", "Alpha"]


def test_names_sort_the_way_a_reader_expects(qapp_or_skip):
    """Case-insensitive, like every other name ordering in the app (the SQL
    ones all say COLLATE NOCASE). Raw str comparison puts every capital
    before every lowercase, so "apple" would sort after "Zebra"."""
    model = _StructuresModel([structure("Zebra"), structure("apple")])
    model.sort(COLUMN["name"], Qt.AscendingOrder)
    assert names(model) == ["apple", "Zebra"]


# ------------------------------------------------- the columns that are not text
def test_deadlines_sort_chronologically_not_alphabetically(qapp_or_skip):
    """The Fuel expires cell reads "2026-09-24 04:00  ·  19d 2h". Sorting the
    displayed string would order by the rendered date text and then by the
    remaining-time suffix, which is not a time order at all."""
    model = _StructuresModel([
        structure("later", fuel_expires="2026-12-01T04:00:00Z"),
        structure("sooner", fuel_expires="2026-09-24T04:00:00Z"),
    ])
    model.sort(COLUMN["fuel_expires"], Qt.AscendingOrder)
    assert names(model) == ["sooner", "later"]


def test_a_structure_with_no_timer_sorts_last(qapp_or_skip):
    """Same rule the column already states in sort_key(): no timer is not the
    most urgent thing on screen."""
    model = _StructuresModel([
        structure("none"),
        structure("dated", fuel_expires="2026-09-24T04:00:00Z"),
    ])
    model.sort(COLUMN["fuel_expires"], Qt.AscendingOrder)
    assert names(model) == ["dated", "none"]


def test_the_vuln_hour_sorts_numerically(qapp_or_skip):
    """Displayed as "09:00", so a text sort happens to work -- until the hour
    is a single digit or absent."""
    model = _StructuresModel([
        structure("nine", reinforce_hour=9),
        structure("two", reinforce_hour=2),
        structure("twenty", reinforce_hour=20),
    ])
    model.sort(COLUMN["reinforce_hour"], Qt.AscendingOrder)
    assert names(model) == ["two", "nine", "twenty"]


# ---------------------------------------------------------------- resetting
def test_resetting_the_sort_restores_the_query_order(qapp_or_skip):
    """View -> Reset sort passes column -1. The query's own order is the
    answer, so the rows have to still be recoverable after a sort."""
    model = _StructuresModel([structure("Zebra"), structure("Alpha")])
    model.sort(COLUMN["name"], Qt.AscendingOrder)
    model.sort(-1, Qt.AscendingOrder)
    assert names(model) == ["Zebra", "Alpha"]


def test_reloading_rows_forgets_the_old_order(qapp_or_skip):
    """set_rows is a fresh query result, not something to re-permute."""
    model = _StructuresModel([structure("Zebra"), structure("Alpha")])
    model.sort(COLUMN["name"], Qt.AscendingOrder)
    model.set_rows([structure("Yankee"), structure("Bravo")])
    assert names(model) == ["Yankee", "Bravo"]


# -------------------------------------------------- through the real control
def test_clicking_the_header_sorts_the_table(qapp_or_skip):
    """End to end through SortController, which is what the header click
    reaches. This is the assertion the previous attempt was missing: the
    controller was attached, so the arrow moved and nothing else did."""
    from evasset.ui.structures_view import StructuresView

    view = StructuresView(defer_load=True)
    view.model.set_rows([structure("Zebra"), structure("Alpha"), structure("Mike")])
    view.sorter._on_header_clicked(COLUMN["name"])
    assert names(view.model) == ["Alpha", "Mike", "Zebra"]


def test_clicking_the_same_header_twice_flips_the_order(qapp_or_skip):
    from evasset.ui.structures_view import StructuresView

    view = StructuresView(defer_load=True)
    view.model.set_rows([structure("Zebra"), structure("Alpha"), structure("Mike")])
    view.sorter._on_header_clicked(COLUMN["name"])
    view.sorter._on_header_clicked(COLUMN["name"])
    assert names(view.model) == ["Zebra", "Mike", "Alpha"]
