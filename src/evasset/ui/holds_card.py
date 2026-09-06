"""The holds card: one consumable, a bay, and how many of it a ship carries.

The question is the one a pilot asks before undocking -- "which of my ships
are short of paste" -- and the grammar can say it
(``holds:"Nanite Repair Paste"<100``), but the three parts of that value are
awkward to type and impossible to browse. The card is the browser: a picker
over the consumables a bay can hold, the bay selector, and the comparison.

The picker's list follows the bay. "All" offers everything sitting directly
inside the estate's ships; "Cargo" what the cargo holds carry; "Fuel",
"Drones" and "Fighters" every fuel, drone or fighter there is, owned or not,
since the question is usually about the ship that has none -- the ones the
ships in scope actually hold lead the list, busiest first, and the rest of
the SDE follows by name; "Fleet" whatever sits in the fleet hangars
(queries.held_type_counts spells out each rule). The card announces
``bay_changed`` and the view re-fetches, so a bay click is also a list
change; whatever name was typed stays in the edit across it, selected when
the new list has it and left as free text when it does not.

Unlike the abyssal card's type picker, this one takes free text. The abyssal
chip can only ever carry a type the estate holds, so its edit is a search box
over the list; a holds chip is a question about a type the ships may hold
none of -- "which ships have fewer than 500 rounds" is most interesting for
the ships with zero -- so any exact SDE name must be typeable whether or not
it is in the list. The entries are bare names for the same reason: the edit
holds the value the chip carries, and a counted label ("12,400 in 6 ships")
in the list would be one more thing a pick had to strip on its way into the
field. The count rides on the entry's tooltip instead.

The card is database-free like its siblings: the view feeds the picker rows
from queries.held_type_counts and answers the footer's count; the card only
builds the chip.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QCompleter,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QWidget,
)

from .. import fitting, omni
from ..omni import (
    HOLDS_KIND,
    HoldsTerm,
    holds_column_key,
    holds_value,
    parse_holds,
)
from . import palette
from .filter_card import FilterCard
from .strip import caption_font

COMPLETIONS_VISIBLE = 12
TYPE_PLACEHOLDER = "Type a consumable, or pick one…"

# The quantity fields' ceiling. A trillion rounds is not a number anyone
# holds, and a spin box needs a finite range.
MAX_QUANTITY = 1_000_000_000
# The decimals a quantity field keeps. A hold count is whole in practice,
# but the grammar accepts any number and a typed `<1.5` seeded into an
# integer spin came back out of Done as `<1`, a filter the user never wrote.
QUANTITY_DECIMALS = 2

# The comparisons the chip's grammar admits, in the order the combo shows
# them; ".." is the two-ended range, which the grammar writes as `=lo..hi`.
OPERATORS = [("<", "<"), ("<=", "≤"), (">", ">"), (">=", "≥"), ("..", "between")]

# A fresh card asks the honest opening question: which ships carry any of the
# consumable at all. The operator swap to "<" -- the ships that are short --
# is one click away, and a default of "<0" would open on a filter matching
# nothing.
DEFAULT_OP, DEFAULT_LOW, DEFAULT_HIGH = ">=", 1, 100

# What each bay button promises, list and count alike; the rules are the
# ones queries.held_type_counts and holds_count_sql apply.
_BAY_TIPS: dict[str | None, str] = {
    None: "Everything sitting directly in the ship, loaded charges included",
    "cargo": "The cargo hold: what your ships carry there",
    "fuel": "The fuel bay and ammo hold: every fuel there is, held or not",
    "drones": "The drone bay: every drone there is, held or not",
    "fighters": "The fighter bay and tubes: every fighter there is, held or not",
    "fleet": "The fleet hangar: whatever your ships carry there",
}


def held_tip(row) -> str:
    """The count a picker entry carries on its tooltip.

    The list no longer shows it inline, and an SDE entry the estate holds
    none of says so instead of a figure -- the same words whether it is a
    fuel, a drone or a fighter, since the list already says which.
    """
    ships = int(row["ships"] or 0)
    if not ships:
        return "None aboard the ships in scope"
    return f"{int(row['units'] or 0):,} in {ships:,} ships"


class QuantitySpin(QDoubleSpinBox):
    """A quantity field that shows "500" and "1.5", never "500.00".

    QDoubleSpinBox pads every value to its decimals, which would dress each
    whole count in a ".00" nobody typed; the value keeps its decimals and
    only the text drops the trailing zeros.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setDecimals(QUANTITY_DECIMALS)
        self.setRange(0, MAX_QUANTITY)
        self.setGroupSeparatorShown(True)

    def textFromValue(self, value: float) -> str:  # noqa: N802
        text = super().textFromValue(value)
        point = self.locale().decimalPoint()
        if point in text:
            text = text.rstrip("0").rstrip(point)
        return text


class HoldsCard(FilterCard):
    """The popover behind the holds chip. See filter_card.py for the commit model."""

    KINDS = (HOLDS_KIND,)
    TITLE = "Ship holds"

    # The bay key (None for the whole ship) after a click on the selector.
    # The list the picker offers is the bay's, so the view answers this with
    # a fresh queries.held_type_counts trip; the count re-ask travels on
    # filter_changed as before.
    bay_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        small = caption_font(self.font())

        hint = QLabel("Consumable")
        hint.setFont(small)
        hint.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.head_layout.addWidget(hint)

        self.type_combo = QComboBox()
        self.type_combo.setEditable(True)
        # The list is a suggestion, never a constraint: a name it does not
        # offer is still a valid question (see the module docstring), so a
        # typed name must not become an entry either.
        self.type_combo.setInsertPolicy(QComboBox.NoInsert)
        tip = "The exact item name — pick one from the bay's list, or type any other"
        self.type_combo.setToolTip(tip)
        self.type_edit = self.type_combo.lineEdit()
        self.type_edit.setToolTip(tip)
        self.type_edit.setPlaceholderText(TYPE_PLACEHOLDER)
        # A plain completer over the combo's own model: an entry's text is
        # its name, so what the popup matches is what a pick writes.
        self._completer = QCompleter(self.type_combo.model(), self.type_combo)
        self._completer.setCompletionMode(QCompleter.PopupCompletion)
        self._completer.setFilterMode(Qt.MatchContains)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setMaxVisibleItems(COMPLETIONS_VISIBLE)
        self.type_combo.setCompleter(self._completer)
        self.type_edit.installEventFilter(self)
        self.type_edit.textEdited.connect(lambda _text: self._on_changed())
        self.type_combo.currentIndexChanged.connect(self._on_type_picked)
        self.head_layout.addWidget(self.type_combo)

        bay_row = QHBoxLayout()
        bay_row.setSpacing(6)
        bay_caption = QLabel("Bay")
        bay_caption.setFont(small)
        bay_caption.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        bay_row.addWidget(bay_caption)
        # Exclusive checkable buttons rather than a combo: six short words
        # that are all worth seeing at once, and the whole-ship total is the
        # one most often wanted, so it must not hide behind a click.
        self.bay_group = QButtonGroup(self)
        self.bay_group.setExclusive(True)
        self.bay_buttons: dict[str | None, QToolButton] = {}
        for key in (None, *fitting.HOLD_BAYS):
            button = QToolButton()
            button.setText("All" if key is None else key.title())
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setToolTip(_BAY_TIPS[key])
            self.bay_group.addButton(button)
            self.bay_buttons[key] = button
            bay_row.addWidget(button)
        bay_row.addStretch(1)
        self.bay_buttons[None].setChecked(True)
        self.bay_group.buttonClicked.connect(self._on_bay_clicked)
        self.body_layout.addLayout(bay_row)

        count_row = QHBoxLayout()
        count_row.setSpacing(6)
        count_caption = QLabel("Carries")
        count_caption.setFont(small)
        count_caption.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        count_row.addWidget(count_caption)
        self.op_combo = QComboBox()
        for op, label in OPERATORS:
            self.op_combo.addItem(label, op)
        self.op_combo.setCurrentIndex(next(
            i for i, (op, _l) in enumerate(OPERATORS) if op == DEFAULT_OP
        ))
        self.op_combo.currentIndexChanged.connect(self._on_operator_changed)
        count_row.addWidget(self.op_combo)
        self.low_spin = QuantitySpin()
        self.high_spin = QuantitySpin()
        self.low_spin.setValue(DEFAULT_LOW)
        self.high_spin.setValue(DEFAULT_HIGH)
        count_row.addWidget(self.low_spin, 1)
        self.range_label = QLabel("to")
        self.range_label.setFont(small)
        self.range_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        count_row.addWidget(self.range_label)
        count_row.addWidget(self.high_spin, 1)
        self.body_layout.addLayout(count_row)
        self._sync_range_widgets()
        # Connected only once every widget the slot touches exists: a
        # setValue during construction would otherwise reach _on_changed
        # before the range label was built.
        for spin in (self.low_spin, self.high_spin):
            spin.valueChanged.connect(lambda _v: self._on_changed())

        self.body_layout.addStretch(1)
        self._update_done_enabled()

    # ------------------------------------------------------------------- API
    def set_types(self, rows) -> None:
        """Fill the picker from queries.held_type_counts rows.

        The rows are (type_id, name, units, ships, owned) and are kept in the
        order they arrive: the query already puts what the ships hold ahead
        of the rest of the SDE, and the completer searches the whole list
        either way. The typed name survives the refill: the list arrives
        after the card is shown and again after every bay click, and a user
        already typing must not have their text replaced by the first
        entry's -- it is re-selected when the new list has it and stands as
        free text when it does not.
        """
        typed = self.type_name()
        self.type_combo.blockSignals(True)
        try:
            self.type_combo.clear()
            for row in rows:
                name = str(row["name"])
                self.type_combo.addItem(name, name)
                self.type_combo.setItemData(
                    self.type_combo.count() - 1, held_tip(row), Qt.ToolTipRole
                )
            self._select_name(typed)
        finally:
            self.type_combo.blockSignals(False)

    def type_name(self) -> str:
        """The consumable's exact name: whatever the edit holds, picked or typed."""
        return self.type_edit.text().strip()

    def bay(self) -> str | None:
        """The selected bay key, or None for the whole ship."""
        return next(
            (key for key, button in self.bay_buttons.items() if button.isChecked()), None
        )

    def term(self):
        """The HoldsTerm the card describes, or None with no name typed.

        An empty name is the one state Done cannot write a chip from.
        """
        name = self.type_name()
        if not name:
            return None
        op = self.op_combo.currentData()
        if op == "..":
            return HoldsTerm(self.bay(), name, "..", float(self.low_spin.value()),
                             float(self.high_spin.value()))
        return HoldsTerm(self.bay(), name, op, float(self.low_spin.value()), None)

    def owns(self, chip) -> bool:
        """Only the positive holds chip this card was seeded from.

        The shell's default would claim every positive holds chip, and Done
        from the second of two would swallow the first; with no seeded term
        (a card opened blank) Done adds its chip beside whatever stands.
        """
        if chip.kind != HOLDS_KIND or chip.negated:
            return False
        seeded = getattr(self, "_seeded_key", None)
        if seeded is None:
            return False
        term = parse_holds(chip.value)
        return term is not None and holds_column_key(term) == seeded

    def seed(self, chips: list) -> None:
        """Take the first positive holds chip's three parts, or open blank.

        A negated holds chip is the complement question, which the card has
        no polarity control for, so it is neither seeded nor replaced -- it
        rides through Done the way a roll: chip rides through the abyssal
        card's."""
        term = next(
            (
                parsed
                for chip in chips
                if chip.kind == HOLDS_KIND and not chip.negated
                for parsed in [parse_holds(chip.value)]
                if parsed is not None
            ),
            None,
        )
        # Done replaces only the chip this card was opened on: two holds chips
        # AND (two thresholds are two requirements), so a card seeded from one
        # must leave the other standing.
        self._seeded_key = holds_column_key(term) if term is not None else None
        self._select_name(term.name if term is not None else "")
        wanted_bay = term.bay if term is not None else None
        self.bay_buttons.get(wanted_bay, self.bay_buttons[None]).setChecked(True)
        op = term.op if term is not None else DEFAULT_OP
        index = self.op_combo.findData(op)
        self.op_combo.blockSignals(True)
        self.op_combo.setCurrentIndex(index if index >= 0 else 0)
        self.op_combo.blockSignals(False)
        low = float(term.low) if term is not None else float(DEFAULT_LOW)
        high = (
            float(term.high) if term is not None and term.high is not None
            else float(DEFAULT_HIGH)
        )
        for spin, value in ((self.low_spin, low), (self.high_spin, high)):
            spin.blockSignals(True)
            spin.setValue(min(max(value, 0.0), float(MAX_QUANTITY)))
            spin.blockSignals(False)
        self._sync_range_widgets()
        self._update_done_enabled()
        self._set_counting()

    def chips(self) -> list:
        """What Done hands back: the one holds chip, or nothing at all."""
        term = self.term()
        return [] if term is None else [omni.Chip(HOLDS_KIND, holds_value(term))]

    def totals(self) -> tuple[str, object]:
        """The footer counts assembled ships.

        Both polarities of the chip are ship-scoped, so "N of TOTAL" is only
        honest against the ships the rest of the filter leaves.
        """
        return "ships", None

    def first_focus(self) -> QWidget:
        return self.type_edit

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.type_edit.selectAll()

    # ------------------------------------------------------------- internals
    def _select_name(self, name: str) -> None:
        """Put a name in the edit, selecting its entry when the list has one.

        Selecting matters for the dropdown's own highlight; the edit shows
        the bare name either way, so a name the estate no longer holds reads
        exactly like one it does.
        """
        index = self.type_combo.findData(name) if name else -1
        self.type_combo.blockSignals(True)
        try:
            self.type_combo.setCurrentIndex(index)
            self.type_edit.setText(name)
        finally:
            self.type_combo.blockSignals(False)

    def _on_type_picked(self, index: int) -> None:
        """A pick from the list writes the entry's name into the edit.

        The edit is the chip's value, and taking it from the data role rather
        than the text keeps that true whatever the entry displays.
        """
        if index >= 0:
            self.type_edit.setText(str(self.type_combo.itemData(index) or ""))
        self._on_changed()

    def _on_bay_clicked(self, _button) -> None:
        self.bay_changed.emit(self.bay())
        self._on_changed()

    def _on_operator_changed(self, _index: int) -> None:
        self._sync_range_widgets()
        self._on_changed()

    def _sync_range_widgets(self) -> None:
        """Show the high field for the range form only, never below the low one.

        parse_stat rejects a reversed range outright, so a chip written from
        one would degrade to bare text and lose the filter.
        """
        between = self.op_combo.currentData() == ".."
        self.range_label.setVisible(between)
        self.high_spin.setVisible(between)
        self.high_spin.setMinimum(self.low_spin.value() if between else 0)

    def _on_changed(self) -> None:
        self._sync_range_widgets()
        self._update_done_enabled()
        self._announce()

    def _update_done_enabled(self) -> None:
        self.done_btn.setEnabled(self.term() is not None)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.type_edit and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                # Enter here commits the text and goes no further: left to
                # travel it would climb to the card, where Enter is Done, and
                # apply whatever half-typed name was standing.
                self._commit_type_text()
                return True
            if key == Qt.Key_Escape:
                # A completion popup takes the first Escape, the card the
                # second -- the abyssal card's ladder.
                popup = self._completer.popup()
                if popup is not None and popup.isVisible():
                    popup.hide()
                else:
                    self.hide()
                return True
        return super().eventFilter(obj, event)

    def _commit_type_text(self) -> None:
        """Enter in the type edit: adopt the matching entry, or keep the text.

        Taking the entry the text names keeps the dropdown in step with the
        field; a name with no entry stays as typed.
        """
        needle = self.type_name().casefold()
        for index in range(self.type_combo.count()):
            name = str(self.type_combo.itemData(index) or "")
            if needle and needle == name.casefold():
                self._select_name(name)
                break
        popup = self._completer.popup()
        if popup is not None and popup.isVisible():
            popup.hide()
        self._on_changed()
