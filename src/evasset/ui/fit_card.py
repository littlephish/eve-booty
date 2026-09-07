"""The fit card: the stored-fit list, and the paste box that grows it.

``fit:"Ratting"`` asks whether a ship is fitted the way it was meant to be,
which needs a fit to compare against -- and there is nowhere else in the app
to put one. So the card is both the picker and the whole store's editor: the
fits already stored, each deletable where it is shown, and a paste box that
takes EFT text from Pyfa or the game client, says what it made of it, and
saves it under a name.

Storing is a deliberate second step rather than a side effect of pasting.
A paste with an item name the SDE does not know cannot be stored at all (a
fit missing a line would report every ship carrying that module as
deviating, which looks authoritative and is wrong), so the status line has
to be read before Save means anything -- and Save is disabled until the
parse is clean.

Both polarities are the card's: ``fit:`` is "matches this fit" and
``-fit:`` is "deviates from it", one radio pair rather than two chips,
because the two questions are asked of the same fit and the hull scope is
the same either way. That is why ``owns`` takes negated chips too, unlike
the shell's default.

The card is database-free like its siblings. Parsing, saving, deleting and
re-listing all belong to the view that owns the connection; the card asks
for them by signal and renders what comes back.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QToolButton,
    QWidget,
)

from .. import omni
from ..omni import FIT_KIND
from . import palette
from .debounce import Debounce
from .filter_card import FilterCard
from .strip import caption_font

PASTE_PLACEHOLDER = "Paste an EFT fit"
NO_HEADER = "No [Hull, Name] header"
LIST_HEIGHT = 132
PASTE_HEIGHT = 96

# The parse runs a query per line against the SDE, so it waits out the paste
# the way every search box in the app waits out typing (debounce.py).
PARSE_DEBOUNCE_MS = 220



def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def parse_status(parsed) -> str:
    """One line saying what the paste amounts to, or what is wrong with it.

    Counts rather than a list, because a fit is fourteen lines and the
    question the line answers is "did it read what I pasted": the module and
    rig totals are the verdict's own multiset size, and the drone and cargo
    figures are counted as TYPES because EFT's ``x400`` quantities are the
    fit's wish, not the ship's contents.
    """
    if parsed is None:
        return ""
    if parsed.hull_type_id is None:
        # A header that was read but whose hull did not resolve to a ship --
        # an unknown name, or a real type of some other category -- would be
        # misreported by NO_HEADER, which sends the user hunting for a
        # bracket that is already there. Echoing the text back is what the
        # user can act on.
        return f"{parsed.hull_name} is not a ship" if parsed.hull_name else NO_HEADER
    if parsed.unknown:
        return "Unknown: " + ", ".join(parsed.unknown)
    counts: dict[str, int] = {}
    for item in parsed.items:
        counts[item.slot_kind] = counts.get(item.slot_kind, 0) + (
            item.quantity if item.slot_kind in ("module", "rig", "subsystem") else 1
        )
    parts = [parsed.hull_name]
    for kind, noun in (
        ("module", "module"), ("rig", "rig"), ("subsystem", "subsystem"),
        ("drone", "drone type"), ("cargo", "cargo type"),
    ):
        if counts.get(kind):
            parts.append(_plural(counts[kind], noun))
    return " · ".join(parts)


class FitCard(FilterCard):
    """The popover behind the fit chip. See filter_card.py for the commit model."""

    KINDS = (FIT_KIND,)
    TITLE = "Stored fits"

    parse_requested = Signal(str)        # EFT text to parse, debounced
    save_requested = Signal(str, str)    # name, EFT text
    delete_requested = Signal(int)       # fit_id

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        small = caption_font(self.font())
        # The name the last parse suggested, so a header change refills the
        # field only while the user has not named the fit themselves.
        self._suggested_name = ""
        self._parsed = None
        self._seed_name: str | None = None

        self.fit_list = QListWidget()
        self.fit_list.setFixedHeight(LIST_HEIGHT)
        self.fit_list.setAlternatingRowColors(True)
        self.fit_list.currentItemChanged.connect(lambda *_: self._on_selection_changed())
        self.head_layout.addWidget(self.fit_list)

        polarity = QHBoxLayout()
        polarity.setSpacing(10)
        self.match_radio = QRadioButton("Matches")
        self.match_radio.setChecked(True)
        self.deviate_radio = QRadioButton("Deviates from")
        self.deviate_radio.setToolTip(
            "The ships of this hull fitted any other way — other hulls are not asked about"
        )
        for radio in (self.match_radio, self.deviate_radio):
            radio.toggled.connect(self._on_polarity_toggled)
            polarity.addWidget(radio)
        polarity.addStretch(1)
        self.head_layout.addLayout(polarity)

        paste_caption = QLabel("Add a fit")
        paste_caption.setFont(small)
        paste_caption.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.body_layout.addWidget(paste_caption)

        self.paste_edit = QPlainTextEdit()
        self.paste_edit.setPlaceholderText(PASTE_PLACEHOLDER)
        self.paste_edit.setFixedHeight(PASTE_HEIGHT)
        self.paste_edit.setTabChangesFocus(True)
        self._parse_debounce = Debounce(self, self._request_parse, interval=PARSE_DEBOUNCE_MS)
        self.paste_edit.textChanged.connect(self._parse_debounce.trigger)
        self.body_layout.addWidget(self.paste_edit)

        self.status_label = QLabel("")
        self.status_label.setFont(small)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.body_layout.addWidget(self.status_label)

        save_row = QHBoxLayout()
        save_row.setSpacing(6)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Name")
        self.name_edit.textEdited.connect(lambda _text: self._update_save_enabled())
        self.name_edit.installEventFilter(self)
        save_row.addWidget(self.name_edit, 1)
        self.save_btn = QPushButton("Save fit")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._on_save)
        save_row.addWidget(self.save_btn)
        self.body_layout.addLayout(save_row)

        self._update_done_enabled()

    # ------------------------------------------------------------------- API
    def set_fits(self, rows, selected_fit_id: int | None = None) -> None:
        """Fill the list from fits.list_fits rows, selecting one by id.

        With no id given, the fit a seed named is selected. The row itself is
        kept on the item so totals() can read the hull without a second
        lookup.
        """
        wanted = selected_fit_id
        if wanted is None:
            current = self.selected_fit()
            if current is not None:
                wanted = int(current["fit_id"])
        self.fit_list.blockSignals(True)
        try:
            self.fit_list.clear()
            for row in rows:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, int(row["fit_id"]))
                item.setData(Qt.UserRole + 1, row)
                widget = self._row_widget(row)
                item.setSizeHint(widget.sizeHint())
                self.fit_list.addItem(item)
                self.fit_list.setItemWidget(item, widget)
            self._select_fit(wanted)
        finally:
            self.fit_list.blockSignals(False)
        self._on_selection_changed()

    def set_parsed(self, parsed) -> None:
        """Render one fits.ParsedFit, or None for an empty paste box.

        Save is armed when the fit can be stored. The name field follows the
        header until the user types their own -- renaming a fit is the point
        of an editable field, and a fresh paste must not silently overwrite
        it.
        """
        self._parsed = parsed
        self.status_label.setText(parse_status(parsed))
        suggested = "" if parsed is None else parsed.name
        if self.name_edit.text().strip() in ("", self._suggested_name):
            self.name_edit.setText(suggested)
        self._suggested_name = suggested
        self._update_save_enabled()

    def selected_fit(self):
        """The selected fits.list_fits row, or None with nothing selected."""
        item = self.fit_list.currentItem()
        return None if item is None else item.data(Qt.UserRole + 1)

    def seed(self, chips: list) -> None:
        """Take the first fit chip: it names the fit to select, and sets the radios.

        The selection happens once the list arrives.
        """
        chip = next((c for c in chips if c.kind == FIT_KIND), None)
        self._seed_name = None if chip is None else chip.value
        self.deviate_radio.blockSignals(True)
        self.match_radio.blockSignals(True)
        self.deviate_radio.setChecked(bool(chip is not None and chip.negated))
        self.match_radio.setChecked(not (chip is not None and chip.negated))
        self.deviate_radio.blockSignals(False)
        self.match_radio.blockSignals(False)
        self.paste_edit.blockSignals(True)
        self.paste_edit.setPlainText("")
        self.paste_edit.blockSignals(False)
        self.name_edit.clear()
        self._suggested_name = ""
        self.set_parsed(None)
        self._select_fit(None)
        self._update_done_enabled()
        self._set_counting()

    def chips(self) -> list:
        """What Done hands back: one fit chip named after the selection.

        Its polarity comes from the radios. With no fit selected there is
        nothing to hand back, which is why Done is disabled then.
        """
        row = self.selected_fit()
        if row is None:
            return []
        return [omni.Chip(FIT_KIND, str(row["name"]), negated=self.deviate_radio.isChecked())]

    def owns(self, chip) -> bool:
        """Both polarities are the card's to rewrite.

        The radios express the negation, so a `-fit:` chip is rewritten
        rather than ridden through.
        """
        return chip.kind in self.KINDS

    def totals(self) -> tuple[str, object]:
        """The footer counts the assembled ships of the selected fit's hull.

        A fit says nothing about any other hull, in either polarity.
        """
        row = self.selected_fit()
        return "ships", None if row is None else int(row["hull_type_id"])

    def first_focus(self) -> QWidget:
        """The list when a fit is already selected, the paste box otherwise.

        QAbstractItemView makes row 0 current when it gains focus with no
        current index, so focusing an unselected list would silently pick
        the first stored fit and enable Done for a user who chose nothing.
        With a selection the list is the right home: the common visit is
        re-picking a fit, and the keyboard lands on it."""
        return self.fit_list if self.selected_fit() is not None else self.paste_edit

    # ------------------------------------------------------------- internals
    def _row_widget(self, row) -> QWidget:
        """One list line: the fit, its hull and size, and the cross that forgets it."""
        widget = QWidget()
        line = QHBoxLayout(widget)
        line.setContentsMargins(4, 2, 2, 2)
        line.setSpacing(6)
        label = QLabel(f"{row['name']} · {row['hull'] or 'unknown hull'}"
                       f" · {_plural(int(row['modules'] or 0), 'module')}")
        line.addWidget(label, 1)
        remove = QToolButton()
        remove.setText("×")
        remove.setAutoRaise(True)
        remove.setCursor(Qt.PointingHandCursor)
        remove.setToolTip("Forget this fit")
        remove.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        fit_id = int(row["fit_id"])
        remove.clicked.connect(lambda _checked=False, i=fit_id: self.delete_requested.emit(i))
        line.addWidget(remove)
        return widget

    def _select_fit(self, fit_id: int | None) -> None:
        """Select a fit by id, falling back to the name a seed carried.

        A fit that is no longer stored leaves nothing selected.
        """
        wanted_name = (self._seed_name or "").casefold() if fit_id is None else None
        for index in range(self.fit_list.count()):
            item = self.fit_list.item(index)
            row = item.data(Qt.UserRole + 1)
            if fit_id is not None and int(item.data(Qt.UserRole)) == fit_id:
                self.fit_list.setCurrentItem(item)
                return
            if wanted_name and str(row["name"]).casefold() == wanted_name:
                self.fit_list.setCurrentItem(item)
                return
        self.fit_list.setCurrentItem(None)

    def _on_selection_changed(self) -> None:
        self._update_done_enabled()
        self._announce()

    def _on_polarity_toggled(self, checked: bool) -> None:
        # Both radios of an exclusive pair report the change; announcing on
        # the one that came on keeps it to a single count per click.
        if checked:
            self._announce()

    def _update_done_enabled(self) -> None:
        self.done_btn.setEnabled(self.selected_fit() is not None)

    def _update_save_enabled(self) -> None:
        parsed = self._parsed
        self.save_btn.setEnabled(
            parsed is not None and parsed.ok and bool(self.name_edit.text().strip())
        )

    def _request_parse(self) -> None:
        text = self.paste_edit.toPlainText()
        if text.strip():
            self.parse_requested.emit(text)
        else:
            self.set_parsed(None)

    def _on_save(self) -> None:
        if self.save_btn.isEnabled():
            self.save_requested.emit(
                self.name_edit.text().strip(), self.paste_edit.toPlainText()
            )

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if (
            obj is self.name_edit
            and event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Return, Qt.Key_Enter)
        ):
            # Enter in the name field means "store it", never Done: a
            # QLineEdit ignores the key, so left to travel it would climb to
            # the card and apply a chip while the user was naming a fit. The
            # paste box needs no such guard -- it takes the newline itself.
            self._on_save()
            return True
        return super().eventFilter(obj, event)
