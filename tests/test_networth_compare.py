"""Charting net worth per owner rather than only in aggregate.

history() either sums every owner together or narrows to exactly one, which
answers "what is all of this worth" and "what is this character worth" but
never "which of them is carrying the account". These cover the uncollapsed
query and the series building on top of it.
"""

from __future__ import annotations

import pytest

from evasset import db, networth

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("PySide6.QtCharts")

from evasset.ui.networth_view import COMPARE_OWNERS, NetWorthView  # noqa: E402


@pytest.fixture
def app():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def snapshot(conn, taken_at, owner_id, total, name):
    conn.execute(
        "INSERT OR IGNORE INTO characters(character_id, name, enabled) VALUES(?,?,1)",
        (owner_id, name),
    )
    conn.execute(
        """INSERT INTO networth_snapshots
               (taken_at, owner_type, owner_id, assets_buy_isk, assets_sell_isk,
                wallet_isk, orders_isk, escrow_isk, contracts_buy_isk,
                contracts_sell_isk, jobs_buy_isk, jobs_sell_isk,
                total_buy_isk, total_sell_isk)
           VALUES(?, 'character', ?, 0,0,0,0,0,0,0,0,0, ?, ?)""",
        (taken_at, owner_id, total * 0.9, total),
    )


@pytest.fixture
def two_characters(tmp_path):
    """Rich Alt is worth more, and Poor Alt was linked later -- so they do not
    share a start date, which is the case the totals have to survive."""
    conn = db.init(tmp_path / "nw.sqlite")
    snapshot(conn, "2026-08-01T00:00:00", 1, 100.0, "Rich Alt")
    snapshot(conn, "2026-08-02T00:00:00", 1, 150.0, "Rich Alt")
    snapshot(conn, "2026-08-02T00:00:00", 2, 10.0, "Poor Alt")
    snapshot(conn, "2026-08-03T00:00:00", 1, 200.0, "Rich Alt")
    snapshot(conn, "2026-08-03T00:00:00", 2, 20.0, "Poor Alt")
    return conn


# --------------------------------------------------------------------- query
def test_history_per_owner_keeps_the_owners_apart(two_characters):
    rows = networth.history_per_owner(two_characters)
    assert len(rows) == 5
    assert {r["owner_name"] for r in rows} == {"Rich Alt", "Poor Alt"}


def test_history_still_sums_when_not_comparing(two_characters):
    """The aggregate view must not change shape because of this."""
    rows = networth.history(two_characters)
    by_date = {r["taken_at"]: r["total_sell_isk"] for r in rows}
    assert by_date["2026-08-03T00:00:00"] == 220.0    # 200 + 20


# ------------------------------------------------------------------- series
def test_compare_mode_draws_one_line_per_owner(app, two_characters):
    view = NetWorthView(defer_load=True)
    view._compare = True

    plots = view._build_series(networth.history_per_owner(two_characters), "sell")

    assert [label for label, _ in plots] == ["Rich Alt", "Poor Alt"]
    rich = dict(plots)["Rich Alt"]
    assert [value for _, value in rich] == [100.0, 150.0, 200.0]
    view.deleteLater()


def test_the_biggest_holding_is_listed_first(app, two_characters):
    """Legend order should match what the eye picks off the chart, rather
    than burying the largest holding at the bottom."""
    view = NetWorthView(defer_load=True)
    view._compare = True

    plots = view._build_series(networth.history_per_owner(two_characters), "sell")

    assert plots[0][0] == "Rich Alt"
    view.deleteLater()


def test_both_totals_collapses_to_one_basis_when_comparing(app, two_characters):
    """Otherwise every owner would get two lines and the comparison drowns."""
    view = NetWorthView(defer_load=True)
    view._compare = True

    plots = view._build_series(networth.history_per_owner(two_characters), "both")

    assert len(plots) == 2, "one line per owner, not one per owner per basis"
    view.deleteLater()


def test_normal_mode_still_plots_components(app, two_characters):
    view = NetWorthView(defer_load=True)
    view._compare = False
    view.breakdown.setChecked(True)

    plots = view._build_series(networth.history(two_characters), "sell")

    labels = [label for label, _ in plots]
    assert "Total" in labels and "Assets" in labels and "Wallet" in labels
    view.deleteLater()


# ------------------------------------------------------------------ totals
def test_ends_take_each_owners_own_first_and_last(two_characters):
    """Poor Alt has no snapshot on 08-01. Anchoring both owners to a shared
    date would either drop them or read their opening as zero, and the
    headline delta would be wrong by their whole balance."""
    rows = networth.history_per_owner(two_characters)

    first, last = NetWorthView._compare_ends(rows, "total_sell_isk")

    assert first == 110.0     # Rich 100 (08-01) + Poor 10 (08-02, their first)
    assert last == 220.0      # Rich 200 + Poor 20


def test_compare_owners_sentinel_cannot_collide_with_a_real_owner():
    """The combo carries (owner_type, owner_id) pairs; this has to be
    distinguishable from every one of them."""
    owner_type, owner_id = COMPARE_OWNERS
    assert owner_type not in ("character", "corporation")
    assert owner_id < 0
