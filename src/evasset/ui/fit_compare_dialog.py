"""Compare deviation: one ship's racks beside a stored fit's, line by line.

The inspector's Fit block already says *that* a ship deviates and lists the
missing and extra modules as text. What it cannot show is where: a pilot
refitting a Dominix wants to look down the low rack of the ship and the low
rack of the fit and see the one line that is not on both. This window puts
the two rack listings side by side, the ship on the left grouped by slot
flag and the fit on the right read out of the stored EFT text's sections,
with the same rack at the same height on both sides. An extra module is
drawn in the verdict red on the ship's side, a missing one in the verdict
green on the fit's side, and a match in the ordinary text colour, so the
deviation is the coloured line and nothing else needs reading.

Under the racks come the Drones and Cargo sections, laid out the same way:
the fit's stacks on the right, what the ship carries on the left. A stack
the ship is short of is the same red-and-green pair -- "0 × Acolyte II (of
5)" opposite "5 × Acolyte II" -- and a type aboard that the fit never
listed is drawn plain, not red, because surplus cargo is not a deviation.
The sections replaced a single "Short: ..." line under the columns: with
a long cargo list that line ran to a paragraph while the columns above it
ended at the rigs with a blank half-window between.

Each line carries its type's icon to the left of the text, the same icon at
the same size the View fit window draws, through the same TypeIconLoader
(type_icons.py): placeholders first, the fetched pixmaps dropped in without
the text moving, and a fetch outlived by the grid -- a reload, or a close --
dropped rather than painted into deleted labels. The fit's own empty-slot
placeholder has no type behind it and gets a blank slot instead, so its
text still starts where every other line's does.

Every line, status and figure comes from fits.compare (Qt-free, tested over
the fit corpus); this file is the chrome. The fit compared is fits.choose_fit's
-- the one a positive `fit:` chip names, else the closest -- so the window
never shows a different fit from the block that opened it.

Non-modal, like InspectorWindow and for the same reason: the user reads it
against the table, and the omnibox must keep working behind it. The
comparison is fetched through the dialog's own AsyncQuery, so a query
still running when the window closes is cancelled rather than delivered
into destroyed widgets (async_query.py's docstring has the segfault
history); a result that slips past the generation guard is dropped by the
closed flag as well, since the test that hands a stale payload straight to
the callback bypasses the guard entirely.
"""

from __future__ import annotations

import html
import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import fits
from . import palette
from .async_query import AsyncQuery
from .strip import _caption_label
from .type_icons import MODULE_ICON_PX, TypeIconLoader, blank_slot

_LOADING = "Comparing with the stored fit…"



def _column_header(title: str, subject: str) -> QWidget:
    """A column head that names its side: the bold role word, then the subject.

    Two lines are what tells a reader glancing at two racks of the same
    modules which one is theirs; a third line repeating the role was
    removed as clutter.
    """
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 4)
    layout.setSpacing(1)
    head = QLabel(f"<b>{html.escape(title)}</b>")
    layout.addWidget(head)
    subject_label = QLabel(html.escape(subject))
    subject_label.setWordWrap(True)
    layout.addWidget(subject_label)
    return box

class FitCompareDialog(QDialog):
    """The two-column comparison for one ship.

    One per ship at a time: the host keeps the open ones by item_id and
    re-uses a window rather than stacking a second copy, so opening the
    same ship twice raises the first window, re-run against whatever fit
    the omnibox now names.
    """

    def __init__(
        self,
        ship_item_id: int,
        ship_name: str,
        hull_type_id: int,
        fit_name: str | None = None,
        parent: QWidget | None = None,
        *,
        defer_load: bool = False,
    ):
        super().__init__(parent, Qt.Window)
        self.ship_item_id = int(ship_item_id)
        self.hull_type_id = int(hull_type_id)
        self.ship_name = ship_name
        self.comparison: fits.FitComparison | None = None
        self._closed = False
        self._icons = TypeIconLoader()
        # (caption, line, label) per rendered line, one list per column; the
        # tests read these rather than walking the grid. The caption is the
        # rack or hold section the line was drawn under, and the label is the
        # text label; its parent is the row that also holds the icon slot.
        self.ship_labels: list[tuple[str, fits.CompareLine, QLabel]] = []
        self.fit_labels: list[tuple[str, fits.CompareLine, QLabel]] = []
        self.captions: list[str] = []
        self.setWindowTitle(f"Compare — {ship_name}")
        self.resize(760, 620)

        root = QVBoxLayout(self)

        head = QHBoxLayout()
        self.title = QLabel(f"<b>{html.escape(ship_name)}</b>")
        head.addWidget(self.title)
        self.verdict = QLabel("")
        verdict_font = self.verdict.font()
        verdict_font.setWeight(QFont.Weight.DemiBold)
        self.verdict.setFont(verdict_font)
        head.addWidget(self.verdict)
        head.addStretch(1)
        self.note = QLabel("")
        self.note.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        head.addWidget(self.note)
        root.addLayout(head)

        self.status = QLabel(_LOADING)
        self.status.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        root.addWidget(self.status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(4, 4, 4, 4)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(24)
        self.grid.setVerticalSpacing(2)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        body_layout.addLayout(self.grid)
        body_layout.addStretch(1)
        scroll.setWidget(self.body)
        root.addWidget(scroll, 1)

        bar = QHBoxLayout()
        self.copied = QLabel("")
        self.copied.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        bar.addWidget(self.copied)
        bar.addStretch(1)
        # The list is what the window is for once the differences are read:
        # everything the fit wants that the ship lacks, as multibuy text, so
        # the next stop is the market window rather than a notepad.
        self.shopping_btn = QPushButton("Copy shopping list")
        self.shopping_btn.setToolTip(
            "Put an EVE multibuy list of every missing module and consumable "
            "shortfall on the clipboard"
        )
        self.shopping_btn.setAutoDefault(False)
        self.shopping_btn.setEnabled(False)
        self.shopping_btn.clicked.connect(self._copy_shopping_list)
        bar.addWidget(self.shopping_btn)
        self.close_btn = QPushButton("Close")
        # Not the dialog's default button: Enter in this window should do
        # nothing, as in InspectorWindow; Esc still closes through reject().
        self.close_btn.setAutoDefault(False)
        self.close_btn.clicked.connect(self.reject)
        bar.addWidget(self.close_btn)
        root.addLayout(bar)

        self._query = AsyncQuery(self)
        if not defer_load:
            self.load(fit_name)

    # ------------------------------------------------------------- loading
    def load(self, fit_name: str | None = None) -> None:
        """Fetch the comparison off the GUI thread and render it when it lands.

        Called again by the host when the window is re-used, so a `fit:` chip
        added since the first open changes what is compared.
        """
        self.status.setText(_LOADING)
        self.status.setVisible(True)
        item_id, hull = self.ship_item_id, self.hull_type_id

        def fetch(conn: sqlite3.Connection) -> fits.FitComparison | None:
            return fits.compare_for_ship(conn, item_id, hull, fit_name)

        self._query.run(fetch, self._on_comparison, self._on_failed)

    def _copy_shopping_list(self) -> None:
        if self.comparison is None:
            return
        text = self.comparison.shopping_list()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        lines = text.count("\n") + 1
        self.copied.setText(f"Copied {lines} line{'s' if lines != 1 else ''} for multibuy.")

    def _on_comparison(self, comparison: fits.FitComparison | None) -> None:
        if self._closed:
            return
        self._clear()
        self.comparison = comparison
        self.copied.setText("")
        self.shopping_btn.setEnabled(comparison is not None and bool(comparison.shopping_list()))
        if comparison is None:
            # The menu action is disabled for a hull with no stored fit, so
            # this is the fit having been deleted between the click and
            # the fetch; say so rather than showing an empty grid.
            self.verdict.setText("")
            self.note.setText("")
            self.status.setText("No stored fit for this hull.")
            return
        self.status.setVisible(False)
        self.setWindowTitle(f"Compare with {comparison.fit_name}")
        self.verdict.setText(comparison.summary)
        self.note.setText(f"compared with {comparison.headline}")
        self._render(comparison)

    def _on_failed(self, message: str) -> None:
        if self._closed:
            return
        self.status.setText(f"Could not compare: {message}")
        self.status.setVisible(True)

    # ----------------------------------------------------------- rendering
    def _clear(self) -> None:
        # Hidden in place, never reparented to None: a visible child handed
        # to setParent(None) becomes a top-level widget for a turn, the
        # focus round trip that closes every popup (the omnibox chip
        # incident, documented on _ChipWidget.prefix_label).
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.ship_labels = []
        self.fit_labels = []
        self.captions = []
        self._icons.reset()

    def _render(self, comparison: fits.FitComparison) -> None:
        """Draw the sections top to bottom, the ship's lines left and the fit's right.

        The racks come first, then Drones and Cargo. A section present on
        only one side still takes its rows on both, so the rig rack of a
        ship with no rigs sits opposite the fit's rig rack rather than
        pulling the subsystems up beside it. The grid is the scroll area's
        only content, so the columns run down as far as the last section
        and no further; nothing here fixes a height.
        """
        row = 0
        # Two-line column headers, because "This ship" over one column and a
        # bare fit name over the other did not say which side was the item
        # under the pointer and which the stored fit it is measured against.
        self.ship_header = _column_header("This ship", f"Inspecting: {self.ship_name}")
        self.fit_header = _column_header(
            "Stored fit", f"Compared against: {comparison.fit_name}"
        )
        self.grid.addWidget(self.ship_header, row, 0)
        self.grid.addWidget(self.fit_header, row, 1)
        row += 1
        for caption_text, left, right in comparison.columns():
            hold = caption_text in fits.HOLD_DISPLAY_ORDER
            self.captions.append(caption_text)
            for column in (0, 1):
                caption = _caption_label(caption_text)
                caption.setContentsMargins(0, 8, 0, 0)
                self.grid.addWidget(caption, row, column)
            row += 1
            for index in range(max(len(left), len(right))):
                if index < len(left):
                    line_row, label = self._line_row(left[index], hold)
                    self.grid.addWidget(line_row, row, 0)
                    self.ship_labels.append((caption_text, left[index], label))
                if index < len(right):
                    line_row, label = self._line_row(right[index], hold)
                    self.grid.addWidget(line_row, row, 1)
                    self.fit_labels.append((caption_text, right[index], label))
                row += 1
        self._icons.start()

    def _line_row(self, line: fits.CompareLine, hold: bool) -> tuple[QWidget, QLabel]:
        """The icon slot and the text label side by side, as in the View fit window.

        The slot is fixed-size either way -- a placeholder awaiting the
        fetch, or blank for a line with no type -- and the text takes the
        rest, so an icon landing late changes pixels and nothing else. The
        icon sits at the top of the row rather than centred, so a long name
        that wraps keeps it beside its first line.
        """
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(12, 0, 0, 0)
        lay.setSpacing(8)
        if line.type_id is None:
            icon = blank_slot(MODULE_ICON_PX)
        else:
            icon = self._icons.slot(line.type_id, MODULE_ICON_PX)
        lay.addWidget(icon, 0, Qt.AlignTop)
        label = self._line_label(line, hold)
        lay.addWidget(label, 1)
        return row, label

    def _line_label(self, line: fits.CompareLine, hold: bool = False) -> QLabel:
        """One line of a rack or a hold section.

        The deviation colour is the verdict pair the roll meters use -- red
        for what should not be on the ship or is short of the fit's figure,
        green for what the fit still wants -- and a placeholder is muted
        text, since it is the fit's own note that the slot is meant to be
        empty. hold says the line is under Drones or Cargo, where an extra
        is surplus and stays in the ordinary text colour.
        """
        if line.status == fits.EMPTY:
            text = html.escape(line.name)
        elif line.status == fits.SHORT:
            text = f"{line.quantity:,} × {html.escape(line.name)} (of {line.wanted:,})"
        else:
            text = f"{line.quantity:,} × {html.escape(line.name)}"
        if line.loaded:
            text += (
                f"  <span style='color: {self._muted_hex()};'>{html.escape(line.loaded)}</span>"
            )
        label = QLabel(text)
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        label.setProperty("status", line.status)
        colour = self.line_colour(line.status, hold)
        if colour is not None:
            label.setStyleSheet(f"color: {colour};")
        return label

    def line_colour(self, status: str, hold: bool = False) -> str | None:
        """The stylesheet colour for a status, or None for the default text.

        Both deviation statuses are answered regardless of column: a match
        is never coloured, so whichever side asks gets the same answer. An
        extra is red in a rack and uncoloured in a hold section, since a
        module the fit did not ask for is a wrong fit and a spare stack of
        paste is not.
        """
        if status == fits.EXTRA:
            return None if hold else palette.delta_hex(False, self.palette())
        if status == fits.SHORT:
            return palette.delta_hex(False, self.palette())
        if status == fits.MISSING:
            return palette.delta_hex(True, self.palette())
        if status == fits.EMPTY:
            return palette.SECONDARY_TEXT
        return None

    def _muted_hex(self) -> str:
        """The muted text colour as hex, for rich text.

        SECONDARY_TEXT is a stylesheet role name, which rich text cannot use;
        the loaded note reads the palette's Shadow (the role SECONDARY_TEXT
        points at, repaired by palette.normalised) as hex.
        """
        return self.palette().color(QPalette.Shadow).name()

    # ------------------------------------------------------------- closing
    def done(self, result: int) -> None:
        """Every close -- Esc, the Close button, the title bar -- lands here.

        The in-flight query is cancelled first so its answer is dropped
        rather than painted into a window that is going away.
        """
        self._closed = True
        self._query.cancel()
        self._icons.cancel()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # QDialog only routes a close through reject() while the window is
        # visible; a hidden dialog closed programmatically must still stop
        # caring about its query.
        self._closed = True
        self._query.cancel()
        self._icons.cancel()
        super().closeEvent(event)
