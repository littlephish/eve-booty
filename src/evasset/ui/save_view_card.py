"""The Save card: naming the working posture so the library can keep it.

``save:Name`` in the omnibox needs no card at all, and that is the fast path
this one exists beside. A name worth typing is usually longer than one
token ("Ships short of paste"), which in the field means remembering to
quote it; here it is an ordinary line edit with the derived label already in
it and selected, so Enter alone is a whole save.

The filter about to be stored is shown as chips in a well, and each chip
carries a cross that leaves it out of the saved view -- not out of the live
omnibox, which the card never touches. A view is often the working filter
minus the one chip that was only ever a detour, and taking it out here is
cheaper than clearing it in the field, saving, and putting it back. Cancel
forgets the removals with everything else. The key cell beside the name
puts the view on a digit in the same save; the hint under it says which
view the digit is being taken from, since a digit holds one view.

Under the well sit the rest of the posture as controls rather than as a
caption: the group-by combo, and the rail's own header row -- its level
combo and the ISK · A-Z · m³ segment, built by the rail's ``LevelSortBar``
so the two are the same widget rather than a likeness. They open on what
the tab is doing now, and like the crosses they edit only what the save
stores: a view is usually the working posture, but the one the pilot wants
to keep is sometimes "this filter, grouped by location, rail on owner by
ISK" while the table is still flat from the last question, and setting
that up in the tab first, saving, and putting the tab back is the same
detour the crosses exist to spare.

Two notices are informational, never blocking. "Same filter as X" says
another view already holds this exact line, because a second copy is
usually a forgotten name rather than a wish; "Replaces X" says the typed
name is taken and the save will overwrite that view in place, which is the
library's rule and the common act of refining a view.

One deliberate departure from the design this card follows: an unfiltered
table is a posture worth saving (the pills stay visible on an empty field
for the same reason), so an empty omnibox shows "Unfiltered" in the well
and Save stays enabled. Only a filter emptied by removing every chip is
"nothing to save" -- the user had a filter and chose to save none of it,
and Cancel is the honest way out of that.

The card is not a filter. It writes no chips, so ``SHOWS_COUNT`` is False --
there is nothing for a footer count to count -- and it overrides ``apply()``
to emit ``save_requested`` rather than the shell's ``done``: a Done that ran
through the view's chip-replacement path would rewrite the very filter it
was asked to preserve.

Database-free like its siblings: the view supplies the state to show, the
group-by choices its own combo offers and the library as it stands, and
hears back one name, one filter line, one slot and the three posture keys.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from .. import omni, queries, views
from . import palette
from .filter_card import FilterCard
from .load_view_card import ChipPill, KeyCell, _flow_area, _rule
from .rail import SORT_KEYS, LevelSortBar
from .strip import caption_font

# The group-by choices the card offers until the view hands over its own
# combo's: the flat table first, then every rollup level, which is how the
# tab builds its combo too (assets_view.py). "" is the flat entry's key for
# the tab's reason -- findData against None is unreliable across bindings.
DEFAULT_GROUP_OPTIONS = [("None", ""), *queries.ROLLUP_LEVELS]

# The well keeps one tall height whatever it holds and scrolls past it: a
# well that hugged its chips read as a one-line field, and the user asked
# for the filter to be the visible centre of the card (mock-up, 2026-09-05).
WELL_MIN_HEIGHT = 120
WELL_MAX_HEIGHT = 120
# The widest a chip in the well may be: the well less the card's margins,
# its border, its padding and the room a scrollbar takes when it overflows.
WELL_CHIP_WIDTH = FilterCard.WIDTH - 24 - 2 - 16 - 16
EMPTIED_TEXT = "No filters — nothing to save"


class SaveViewCard(FilterCard):
    """The popover behind ``save:``, the Save pill and Ctrl+S. See filter_card.py."""

    KINDS = ()
    TITLE = "Save view"
    SHOWS_COUNT = False

    # The name, the filter line to store (the omnibox's minus any chip the
    # well's crosses took out), the digit to put it on (or None to leave
    # whatever slot a replaced view already has), then the posture as the
    # controls left it: the group-by key ("" for flat), the rail level key
    # and the rail sort key.
    save_requested = Signal(str, str, object, str, str, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        pal = self.palette()
        small = caption_font(self.font())
        self._state = views.ViewState()
        # The tokens the save will write: the omnibox's chips and bare words
        # as the card was opened, less the ones crossed out since.
        self._tokens: list = []
        self._existing: list = []
        self._slot: int | None = None
        # The name the last render suggested, so a changed suggestion refills
        # the field only while the user has not named the view themselves.
        self._suggested = ""
        self.done_btn.setText("Save view")
        self.done_btn.setToolTip("Save (Enter)")

        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(0)
        self.body_layout.addWidget(_rule(pal))
        body = QWidget()
        form = QGridLayout(body)
        form.setContentsMargins(12, 10, 12, 10)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        self.body_layout.addWidget(body)

        caption_row = QHBoxLayout()
        filter_caption = QLabel("Filter")
        filter_caption.setFont(small)
        filter_caption.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        caption_row.addWidget(filter_caption, 1)
        self.copy_btn = QToolButton()
        self.copy_btn.setText("Copy as text")
        self.copy_btn.setFont(small)
        self.copy_btn.setAutoRaise(True)
        self.copy_btn.setFocusPolicy(Qt.NoFocus)
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.setToolTip("Put the filter line on the clipboard to share")
        self.copy_btn.setStyleSheet(
            "QToolButton { border: none; border-radius: 2px; padding: 0 6px; }"
            f" QToolButton:hover {{ background: {palette.toward_text(pal, 0.08).name()}; }}"
        )
        self.copy_btn.clicked.connect(self._on_copy)
        caption_row.addWidget(self.copy_btn)
        form.addLayout(caption_row, 0, 0, 1, 2)

        self.well, self.well_flow = _flow_area()
        self.well.setObjectName("well")
        self.well.setStyleSheet(
            "#well { background: palette(base);"
            f" border: 1px solid {palette.track_colour(pal).name()}; border-radius: 2px; }}"
        )
        self.well_flow.setContentsMargins(8, 6, 8, 6)
        self.well.setMinimumHeight(WELL_MIN_HEIGHT)
        self.well.setMaximumHeight(WELL_MAX_HEIGHT)
        form.addWidget(self.well, 1, 0, 1, 2)

        # The posture controls, in a grid of their own so their captions
        # share one column without widening the key cell's column below,
        # which is sized to the 44px cell.
        posture = QGridLayout()
        posture.setContentsMargins(0, 2, 0, 0)
        posture.setHorizontalSpacing(8)
        posture.setVerticalSpacing(6)
        group_caption = QLabel("Group by")
        group_caption.setFont(small)
        group_caption.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        posture.addWidget(group_caption, 0, 0)
        self.group_combo = QComboBox()
        self._set_group_options(DEFAULT_GROUP_OPTIONS)
        posture.addWidget(self.group_combo, 0, 1)
        rail_caption = QLabel("Rail")
        rail_caption.setFont(small)
        rail_caption.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        posture.addWidget(rail_caption, 1, 0)
        self.rail_bar = LevelSortBar()
        posture.addWidget(self.rail_bar, 1, 1)
        posture.setColumnStretch(1, 1)
        form.addLayout(posture, 2, 0, 1, 2)

        self.dupe_label = QLabel("")
        self.dupe_label.setFont(small)
        self.dupe_label.setWordWrap(True)
        self.dupe_label.setStyleSheet(f"color: {palette.status_hex(palette.WARN, pal)};")
        self.dupe_label.setVisible(False)
        form.addWidget(self.dupe_label, 3, 0, 1, 2)

        key_caption = QLabel("Key")
        key_caption.setFont(small)
        key_caption.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        form.addWidget(key_caption, 4, 0)
        name_caption = QLabel("Name")
        name_caption.setFont(small)
        name_caption.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        form.addWidget(name_caption, 4, 1)
        self.key_cell = KeyCell(28)
        self.key_cell.slot_chosen.connect(self._on_slot_chosen)
        form.addWidget(self.key_cell, 5, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setFixedHeight(28)
        self.name_edit.textEdited.connect(lambda _text: self._refresh_notices())
        form.addWidget(self.name_edit, 5, 1)
        form.setColumnStretch(1, 1)

        self.replace_label = QLabel("")
        self.replace_label.setFont(small)
        self.replace_label.setWordWrap(True)
        self.replace_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.replace_label.setVisible(False)
        form.addWidget(self.replace_label, 6, 0, 1, 2)

        self.conflict_label = QLabel("")
        self.conflict_label.setFont(small)
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.conflict_label.setVisible(False)
        form.addWidget(self.conflict_label, 7, 0, 1, 2)

        self.status_label = QLabel("")
        self.status_label.setFont(small)
        self.status_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.footer_layout.insertWidget(0, self.status_label)

        self._render_well()
        self._refresh_notices()

    # ------------------------------------------------------------------- API
    def set_view(self, state, existing: list, group_options: list | None = None) -> None:
        """Render the view about to be saved against the library as it stands.

        The chips of `state.filter` go in the well, the posture controls
        under it are set to `state`'s group-by, rail level and rail sort,
        the digits `existing` views hold go in the key menu, and the notices
        that need the other views' names and lines are refreshed.

        `group_options` is the tab's own combo as (label, key) pairs, so
        the card can never offer a grouping the tab does not have; left
        out, the card's default list (the same construction) stands.

        The suggested name yields to a typed one, the fit card's rule: a
        user who has named the view must not have it renamed under them by
        a second fetch, while an untouched field keeps following the
        filter. The posture controls follow the state each time instead --
        the card is fetched once per opening, so there is no second fetch
        to overwrite a choice made here.
        """
        self._state = state
        self._existing = list(existing)
        spec = omni.parse(state.filter)
        self._tokens = [*spec.chips, *spec.text.split()]
        self.key_cell.set_taken(self._taken())
        if group_options is not None:
            self._set_group_options(group_options)
        index = self.group_combo.findData(state.group_by or "")
        self.group_combo.setCurrentIndex(max(index, 0))
        self.rail_bar.set_level(state.rail_level)
        self.rail_bar.set_sort(state.rail_sort)
        count = len(self._existing)
        self.status_label.setText(f"{count} saved view" + ("" if count == 1 else "s"))
        self._render_well()
        self._refresh_notices()

    def name(self) -> str:
        """The name the save goes under.

        The typed one, or the suggestion the placeholder shows when the field
        was left blank.
        """
        return self.name_edit.text().strip() or self._suggested

    def filter_line(self) -> str:
        """The one-line grammar the save stores: the tokens still in the well."""
        chips = [t for t in self._tokens if isinstance(t, omni.Chip)]
        words = [t for t in self._tokens if not isinstance(t, omni.Chip)]
        return omni.FilterSpec(text=" ".join(words), chips=chips).to_text()

    def slot(self) -> int | None:
        return self._slot

    def group_by(self) -> str:
        """The group-by key the save stores, "" for the flat table."""
        return self.group_combo.currentData() or ""

    def rail_level(self) -> str:
        return self.rail_bar.current_level()

    def rail_sort(self) -> str:
        return self.rail_bar.current_sort()

    def tokens(self) -> list:
        return list(self._tokens)

    def pills(self) -> list[ChipPill]:
        return [
            self.well_flow.itemAt(i).widget()
            for i in range(self.well_flow.count())
            if isinstance(self.well_flow.itemAt(i).widget(), ChipPill)
        ]

    def well_text(self) -> str:
        """What the well says when it holds no chips, or "" while it does."""
        widget = self.well_flow.itemAt(0).widget() if self.well_flow.count() else None
        return widget.text() if isinstance(widget, QLabel) else ""

    # ------------------------------------------------------ subclass contract
    def seed(self, chips: list) -> None:
        """Start over for a fresh visit.

        The name, the key, the removals and the status line belong to one
        visit; the state itself arrives from the fetch that follows.
        """
        self._slot = None
        self.key_cell.set_slot(None)
        self.status_label.clear()
        self._tokens = []
        # The posture controls go back to the tab's defaults too, so a
        # state that names no level or sort (the empty ViewState of a
        # standalone card) does not inherit the last visit's choices.
        self.group_combo.setCurrentIndex(0)
        self.rail_bar.set_level(queries.ROLLUP_LEVELS[0][1])
        self.rail_bar.set_sort(SORT_KEYS[0])
        self._render_well()
        # After the render, which suggests a name for the empty well: the
        # field opens blank and the fetch's set_view fills it.
        self.name_edit.clear()
        self._suggested = ""
        self._refresh_notices()

    def chips(self) -> list:
        """Nothing: a command card leaves the filter exactly as it found it."""
        return []

    def owns(self, chip) -> bool:
        return False

    def totals(self) -> tuple[str, object]:
        return "none", None

    def first_focus(self) -> QWidget:
        """The name field, with the suggestion selected.

        The first keystroke then replaces the suggestion and Enter alone
        accepts it.
        """
        self.name_edit.selectAll()
        return self.name_edit

    def apply(self) -> None:
        """Save view: ask for the save by name, line, key and posture.

        ``done`` is never emitted -- the shell's signal means "replace my
        kinds in the filter", and this card has none. Enter in the name
        field arrives here too: a QLineEdit ignores Return once it has
        emitted returnPressed, so the key climbs to the shell's
        keyPressEvent, and no event filter is needed to make Enter save.
        """
        if not self.done_btn.isEnabled():
            return
        self._applied = True
        self.hide()
        self.save_requested.emit(
            self.name(), self.filter_line(), self._slot,
            self.group_by(), self.rail_level(), self.rail_sort(),
        )

    # ------------------------------------------------------------- internals
    def _taken(self) -> dict[int, str]:
        return {int(v.slot): v.name for v in self._existing if v.slot is not None}

    def _set_group_options(self, options: list) -> None:
        """Refill the group-by combo from (label, key) pairs when they differ.

        A refill resets the selection, and set_view sets that right after
        anyway, so an unchanged list is left alone.
        """
        current = [
            (self.group_combo.itemText(i), self.group_combo.itemData(i))
            for i in range(self.group_combo.count())
        ]
        if current == [(str(label), key) for label, key in options]:
            return
        self.group_combo.clear()
        for label, key in options:
            self.group_combo.addItem(str(label), key)

    def _emptied(self) -> bool:
        """Whether every chip was crossed out of a filter that had some.

        That is the one state with nothing to save (see the module
        docstring).
        """
        return bool(self._state.filter.strip()) and not self._tokens

    def _render_well(self) -> None:
        flow = self.well_flow
        # Hidden in place, never reparented to None (the Load card's
        # _clear_layout has the incident and the numbers).
        while flow.count():
            item = flow.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        inner = self.well.widget()
        if self._tokens:
            for token in self._tokens:
                pill = ChipPill(token, removable=True, max_width=WELL_CHIP_WIDTH, parent=inner)
                pill.removed.connect(lambda t=token: self._remove_token(t))
                flow.addWidget(pill)
        else:
            empty = QLabel(EMPTIED_TEXT if self._emptied() else views.UNFILTERED, inner)
            empty.setFont(caption_font(self.font()))
            empty.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
            flow.addWidget(empty)
        # Nothing to share on an unfiltered view: an enabled button that
        # copies an empty string and then says "Copied." would report a copy
        # that put nothing on the clipboard.
        self.copy_btn.setEnabled(bool(self._tokens))
        # The well holds its one tall height (the constants agree today) and
        # scrolls when the chips outgrow it; the flow is still measured so a
        # future cap above the floor needs nothing but the constant changed.
        margins = flow.contentsMargins()
        width = self.WIDTH - 24 - 2 - margins.left() - margins.right()
        wanted = flow.heightForWidth(width) + 2
        self.well.setFixedHeight(max(WELL_MIN_HEIGHT, min(WELL_MAX_HEIGHT, wanted)))
        self._refresh_suggestion()

    def _remove_token(self, token) -> None:
        """A cross on a chip: out of the view being saved, never out of the omnibox.

        Identity rather than equality, so one of two equal chips goes rather
        than both.
        """
        self._tokens = [t for t in self._tokens if t is not token]
        self._render_well()
        self._refresh_notices()

    def _refresh_suggestion(self) -> None:
        chips = [t for t in self._tokens if isinstance(t, omni.Chip)]
        words = [t for t in self._tokens if not isinstance(t, omni.Chip)]
        suggested = views.suggest_name(omni.FilterSpec(text=" ".join(words), chips=chips))
        if self.name_edit.text().strip() in ("", self._suggested):
            self.name_edit.setText(suggested)
        self._suggested = suggested
        self.name_edit.setPlaceholderText(suggested)

    def _on_slot_chosen(self, slot) -> None:
        self._slot = slot
        self.key_cell.set_slot(slot)
        self._refresh_notices()
        self.name_edit.setFocus()

    def _on_copy(self) -> None:
        """Put the one-line grammar on the clipboard.

        A view is shareable as the text it is made of, and the Load card's
        paste box is the other end.
        """
        QGuiApplication.clipboard().setText(self.filter_line())
        self.status_label.setText("Copied.")

    def _refresh_notices(self) -> None:
        name = self.name()
        typed = name.casefold()
        match = next((v for v in self._existing if v.name.casefold() == typed), None)
        # The stored spelling, not the typed one: a save under a name
        # differing only in case replaces the row and keeps its own case, so
        # the notice has to show which row that is.
        self.replace_label.setText("" if match is None else f"Replaces “{match.name}”")
        self.replace_label.setVisible(match is not None)

        line = self.filter_line()
        twin = next(
            (
                v for v in self._existing
                if line and v.state.filter.strip() == line and v.name.casefold() != typed
            ),
            None,
        )
        self.dupe_label.setText("" if twin is None else f"Same filter as “{twin.name}”")
        self.dupe_label.setVisible(twin is not None)

        holder = self._taken().get(self._slot) if self._slot is not None else None
        conflict = holder is not None and holder.casefold() != typed
        self.conflict_label.setText(
            f"{self._slot} is currently “{holder}” — saving moves it to this view"
            if conflict else ""
        )
        self.conflict_label.setVisible(conflict)
        self._update_done_enabled()

    def _update_done_enabled(self) -> None:
        self.done_btn.setEnabled(bool(self.name()) and not self._emptied())
