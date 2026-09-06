"""The Load card: the view library, a preview of the one picked, and a paste box.

``load:Name`` recalls a view the user can already name; this card is for the
other question -- "what did I save?" -- so it is the library's list, its
editor and a preview at once. The list on the left carries only what tells
two views apart at a glance, the name and the digit that recalls it; the
pane on the right shows what the selected one is made of, as chips, and
what loading it would do to the filter in the omnibox right now as a token
diff: the chips a load drops first, because that is the cost, then the
shared ones, then what it brings in. A load is not additive, and the chips
about to be thrown away are worth seeing before Load view.

Editing lives beside the thing edited. A slot is assigned from the row's
own key cell (and comes off whatever view held it -- the store's rule, the
card only asks), a name is corrected in the pane's header, and Delete there
forgets the view and frees its slot. The alternative, a settings page for
saved views, would be a second place to look for something the list already
shows.

The paste box, pinned under the list, is the receiving end of the Save
card's Copy as text: a filter line arrives from a fleetmate as one line of
the same grammar the omnibox writes and becomes a view, unslotted, under
the name typed or the one the store suggests from the line.

The card owns the keyboard while it is up. ↑/↓ move the selection, a digit
selects and loads the view on that key, Enter outside an edit does nothing
(see keyPressEvent), and Esc closes an open key menu or an inline rename
before it closes the card. The list
itself takes no focus at all, which is how the card keeps the fit card's
rule -- a QAbstractItemView makes row 0 current the moment it gains focus,
and a Done armed by a selection nobody made would throw the working filter
away.

Two pieces here serve the Save card too, which imports them rather than
growing twins: ``ChipPill``, the read-only chip in the omnibox's own dress
(muted prefix, the value, the kind's wash), and ``KeyCell``, the slot cell
with its ten-item menu. They live in this module because the library is the
Load card's subject and the slot menu's semantics -- one view per digit,
taking a digit moves it -- are the list's.

Database-free like its siblings: listing, saving, re-slotting, renaming,
deleting and importing all belong to the view that owns the connection; the
card asks by signal and renders what comes back.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import omni, queries, views
from . import palette
from .filter_card import FilterCard
from .flow_layout import FlowLayout
from .omnibox import abyssal_chip_label
from .rail import SORT_LABELS
from .strip import caption_font

_LEVEL_LABELS = {key: label for label, key in queries.ROLLUP_LEVELS}
_SORT_DISPLAY = dict(SORT_LABELS)

CARD_WIDTH = 640
# The two-pane body has one fixed height: the card never grows with the
# library, the list scrolls instead, so the footer stays where the hand
# expects it however many views there are.
BODY_HEIGHT = 420
LIST_WIDTH = 250
ROW_HEIGHT = 32
CHIP_HEIGHT = 20
# The widest a chip in the preview pane may be: the pane less its margins
# and the room a scrollbar takes when the region overflows.
PANE_CHIP_WIDTH = CARD_WIDTH - LIST_WIDTH - 2 - 24 - 16

NO_SLOT = "–"
PASTE_PLACEHOLDER = "Paste a filter"
NAME_PLACEHOLDER = "Name (optional)"
EMPTY_TEXT = "Select a view to preview it"
IDENTICAL_TEXT = "Identical to the current filter"
DIFF_CAPTION = "VS CURRENT FILTER"

# The washes under the diff signs. A drop leans on the same red the roll
# meters use for a bad roll and an addition on their green, both measured
# in tests/test_contrast.py under default text; a shared token keeps its
# own kind's wash, faded toward the window so the eye reads it as
# unchanged. The sign sits in a fixed-width slot so the chips align
# whether the glyph is a minus, an equals or a plus.
_SIGN_WIDTH = 8
_SHARED_FADE = 0.45
_DIGIT_KEYS = {getattr(Qt, f"Key_{n}"): n for n in views.SLOTS}


def _hex(colour: QColor) -> str:
    return colour.name()


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """The colour a fraction of the way from a to b, the toward_text arithmetic."""
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def token_wash(token, sign: str | None, pal) -> str:
    """The background hex for one chip in the cards.

    The omnibox's own wash for a plain chip, the roll meters' red and green
    under a "-" and a "+", and a faded kind wash under an "=". A bare word
    wears the accent wash, the one chip_tint hands any kind it has never
    heard of.
    """
    if sign == "-":
        return palette.quality_tint(0.0, pal) or palette.chip_tint("", True, pal)
    if sign == "+":
        return palette.quality_tint(1.0, pal) or palette.chip_tint("", False, pal)
    if isinstance(token, omni.Chip):
        wash = palette.chip_tint(token.kind, token.negated, pal)
    else:
        wash = palette.chip_tint("", False, pal)
    if sign == "=":
        return _hex(_mix(QColor(wash), pal.color(QPalette.Window), _SHARED_FADE))
    return wash


def describe_posture(state) -> str:
    """The non-filter half of a view as one caption, or "" when it records none.

    The caption reads "Group by Location · Rail Owner · ISK"; an import, or
    a view saved before the columns existed, records none of it. The rail's
    sort rides on the rail's level rather than taking a word of its own, the
    way the rail's own caption writes "All · by ISK".
    """
    parts = []
    if state.group_by:
        parts.append(f"Group by {_LEVEL_LABELS.get(state.group_by, state.group_by)}")
    if state.rail_level:
        parts.append(f"Rail {_LEVEL_LABELS.get(state.rail_level, state.rail_level)}")
    if state.rail_sort:
        parts.append(_SORT_DISPLAY.get(state.rail_sort, state.rail_sort))
    return " · ".join(parts)


def token_labels(token) -> tuple[str, str]:
    """The (prefix, value) a chip pill shows, in the omnibox's own spelling.

    The grammar's short prefix with a leading minus for a negated chip, the
    abyssal chip's summary label with no prefix at all, and a bare search
    word as itself with nothing before it.
    """
    if not isinstance(token, omni.Chip):
        return "", str(token)
    sign = "-" if token.negated else ""
    if token.kind == omni.ABYSSAL_KIND:
        return sign, abyssal_chip_label(token)
    return f"{sign}{omni.prefix_for_kind(token.kind)}:", token.value


class _ElidedLabel(QLabel):
    """A label that cuts its text with an ellipsis rather than growing.

    The name column has one width and a view name has no upper bound.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        hint.setWidth(24)
        return hint

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        elided = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.width())
        painter.drawText(self.rect(), int(Qt.AlignLeft | Qt.AlignVCenter), elided)


class ChipPill(QFrame):
    """One read-only filter token in the omnibox chip's dress.

    A muted prefix, the value, the kind's wash; optionally a fixed sign slot
    before the prefix (the diff block) or a cross after the value (the Save
    card's well, where the cross takes the token out of the view being
    saved).

    A frame rather than a reuse of the omnibox's own widget: that one is
    wired for removal from the live filter and carries the card glyph, and
    both would be wrong here.
    """

    removed = Signal()

    def __init__(
        self,
        token,
        *,
        sign: str | None = None,
        removable: bool = False,
        max_width: int | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.token = token
        self.sign = sign
        pal = self.palette()
        self.setObjectName("chippill")
        self.setFixedHeight(CHIP_HEIGHT)
        if max_width is not None:
            # A station name can outrun the whole pane; the pill stops at
            # the region's width and the value elides rather than the
            # chip spilling out of the card. The tooltip carries the rest.
            self.setMaximumWidth(max_width)
        self.setStyleSheet(
            f"#chippill {{ background: {token_wash(token, sign, pal)};"
            f" border-radius: {CHIP_HEIGHT // 2}px; }}"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(7, 0, 4 if removable else 7, 0)
        row.setSpacing(2)

        small = caption_font(self.font())
        self.sign_label: QLabel | None = None
        if sign is not None:
            self.sign_label = QLabel(sign, self)
            self.sign_label.setFont(small)
            self.sign_label.setFixedWidth(_SIGN_WIDTH)
            self.sign_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
            row.addWidget(self.sign_label)
            row.addSpacing(2)

        prefix, value = token_labels(token)
        # Parented on creation, the omnibox chip's rule: a parentless label
        # shown for a moment becomes a top-level window, and on Windows that
        # round trip of focus closes every popup -- this card included.
        self.prefix_label = QLabel(prefix, self)
        self.prefix_label.setFont(small)
        self.prefix_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        row.addWidget(self.prefix_label)
        self.prefix_label.setVisible(bool(prefix))
        self.value_label = _ElidedLabel(value, self)
        self.value_label.setFont(small)
        self.value_label.setToolTip(value)
        row.addWidget(self.value_label)

        self.close_btn: QToolButton | None = None
        if removable:
            self.close_btn = QToolButton(self)
            self.close_btn.setText("×")
            self.close_btn.setFixedSize(14, 14)
            self.close_btn.setAutoRaise(True)
            self.close_btn.setCursor(Qt.PointingHandCursor)
            self.close_btn.setToolTip("Leave this filter out of the saved view")
            self.close_btn.setStyleSheet(
                "QToolButton { border: none; border-radius: 7px; padding: 0;"
                f" color: {palette.SECONDARY_TEXT}; }}"
                " QToolButton:hover {"
                f" background: {_hex(palette.toward_text(pal, 0.12))};"
                " color: palette(window-text); }"
            )
            self.close_btn.clicked.connect(lambda _checked=False: self.removed.emit())
            row.addWidget(self.close_btn)


class _KeyMenu(QFrame):
    """The ten-item menu under a key cell: "–" and the nine digits.

    The digit in force is highlighted and a digit another view holds is
    muted with a tooltip naming it. A popup of plain buttons rather than a
    QMenu because a QMenu cannot mute one action and tooltip it; ten
    buttons in a column can.

    One menu serves every key cell that shares it. The Load card has a row
    per view and a menu is eleven widgets with a popup window behind them:
    built per row, a 200-view library cost some 430 ms on every open and
    again after every slot, rename or delete (measured offscreen on
    2026-09-05), nearly all of it in menus nobody had opened. The cell that
    opens the menu is remembered until the pick, and ``chosen`` names it,
    so a shared menu answers to whichever cell asked.
    """

    chosen = Signal(object, object)  # the cell that asked, and the slot picked (None for "–")

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.Popup)
        self.asker: KeyCell | None = None
        pal = self.palette()
        self.setObjectName("keymenu")
        self.setFixedWidth(44)
        self.setStyleSheet(
            "#keymenu { background: palette(window);"
            f" border: 1px solid {_hex(palette.track_colour(pal))}; border-radius: 2px; }}"
        )
        column = QVBoxLayout(self)
        column.setContentsMargins(1, 2, 1, 2)
        column.setSpacing(0)
        self.buttons: dict[int | None, QToolButton] = {}
        for slot in (None, *views.SLOTS):
            button = QToolButton(self)
            button.setText(NO_SLOT if slot is None else str(slot))
            button.setFixedHeight(22)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setAutoRaise(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, s=slot: self._pick(s))
            column.addWidget(button)
            self.buttons[slot] = button

    def open_for(self, current: int | None, taken: dict[int, str], anchor: KeyCell) -> None:
        """Show under `anchor` with `current` highlighted and taken digits muted.

        `taken` maps slot to the holder's name; picking one of those moves
        the digit here, which the tooltip says before the click. The anchor
        is the cell the pick is for until the menu is next opened.
        """
        self.asker = anchor
        pal = self.palette()
        hover = _hex(palette.toward_text(pal, 0.06))
        active = _hex(palette.toward_text(pal, 0.10))
        base = (
            "QToolButton { border: none; border-radius: 0; text-align: left;"
            " padding: 0 6px; %s }"
            f" QToolButton:hover {{ background: {hover}; }}"
        )
        for slot, button in self.buttons.items():
            holder = taken.get(slot) if slot is not None else None
            if slot == current:
                button.setStyleSheet(
                    base % f"background: {active}; color: palette(highlight);"
                )
            elif holder is not None:
                button.setStyleSheet(base % f"color: {palette.SECONDARY_TEXT};")
            else:
                button.setStyleSheet(base % "")
            button.setToolTip(
                f"Take from “{holder}”" if holder is not None and slot != current else ""
            )
        self.adjustSize()
        self.move(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))
        self.show()
        self.setFocus()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Esc closes the menu and stops: the card underneath must not read
        # the same press as its own Esc and close too.
        if event.key() == Qt.Key_Escape:
            self.hide()
            event.accept()
            return
        if event.key() in _DIGIT_KEYS:
            self._pick(_DIGIT_KEYS[event.key()])
            event.accept()
            return
        if event.key() in (Qt.Key_Minus, Qt.Key_0, Qt.Key_Delete, Qt.Key_Backspace):
            self._pick(None)
            event.accept()
            return
        super().keyPressEvent(event)

    def _pick(self, slot: int | None) -> None:
        self.hide()
        self.chosen.emit(self.asker, slot)


class KeyCell(QToolButton):
    """The slot cell on a row or in the Save card.

    It shows the digit (or "–", muted when unbound) with a ▾ and opens the
    key menu on click. Clicking never changes which row is selected -- the
    button consumes the press before the row sees it.

    A cell built with no ``menu`` owns one, the Save card's case; the Load
    card hands every row's cell the one menu it owns (see _KeyMenu) and
    routes that menu's picks back to the cell that opened it.
    """

    slot_chosen = Signal(object)  # slot or None

    def __init__(
        self, height: int = 22, parent: QWidget | None = None, menu: _KeyMenu | None = None
    ):
        super().__init__(parent)
        self._slot: int | None = None
        self._taken: dict[int, str] = {}
        self.setFixedSize(44, height)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(
            "Key: Ctrl+digit loads this view from anywhere on the tab; pick the digit here"
        )
        self.setFocusPolicy(Qt.NoFocus)
        if menu is None:
            menu = _KeyMenu(self)
            menu.chosen.connect(lambda _cell, slot: self._on_chosen(slot))
        self.menu = menu
        self.clicked.connect(self.open_menu)
        self.set_slot(None)

    # ------------------------------------------------------------------- API
    def slot(self) -> int | None:
        return self._slot

    def label(self) -> str:
        """The digit shown, or "–", without the ▾ that shares the button's text."""
        return NO_SLOT if self._slot is None else str(self._slot)

    def set_slot(self, slot: int | None) -> None:
        self._slot = slot
        self.setText(f"{self.label()} ▾")
        pal = self.palette()
        colour = palette.SECONDARY_TEXT if slot is None else "palette(window-text)"
        self.setStyleSheet(
            f"QToolButton {{ border: 1px solid {_hex(palette.track_colour(pal))};"
            " border-radius: 2px; background: palette(window); padding: 0 4px;"
            f" text-align: left; color: {colour}; }}"
            f" QToolButton:hover {{ background: {_hex(palette.toward_text(pal, 0.06))}; }}"
        )

    def set_taken(self, taken: dict[int, str]) -> None:
        """Which digits other views hold, slot -> name, for the menu."""
        self._taken = dict(taken)

    def open_menu(self) -> None:
        self.menu.open_for(self._slot, self._taken, self)

    def pick(self, slot: int | None) -> None:
        """Choose without the menu, as a test does and a key on the row could."""
        self._on_chosen(slot)

    # ------------------------------------------------------------- internals
    def _on_chosen(self, slot: int | None) -> None:
        if slot == self._slot:
            return
        self.slot_chosen.emit(slot)


class _ViewRow(QWidget):
    """One library line: the key cell and the name.

    Paints its own hover and selection washes -- the row widget covers its
    list item, so the list's own :hover and :selected never see the mouse
    -- with the 2px accent bar on the left that marks the selected row.
    """

    pressed = Signal(int)                # view_id: select me
    rename_asked = Signal(int)           # view_id: a double click
    slot_chosen = Signal(int, object)    # view_id, slot or None

    def __init__(self, view, parent: QWidget | None = None, menu: _KeyMenu | None = None):
        super().__init__(parent)
        self._selected = False
        self.setFixedHeight(ROW_HEIGHT)
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)

        line = QHBoxLayout(self)
        line.setContentsMargins(10, 0, 10, 0)
        line.setSpacing(8)
        self.key_cell = KeyCell(22, self, menu)
        self.key_cell.slot_chosen.connect(
            lambda slot: self.slot_chosen.emit(self.view_id, slot)
        )
        line.addWidget(self.key_cell)
        self.name_label = _ElidedLabel("", self)
        line.addWidget(self.name_label, 1)
        self.set_view(view)

    def set_view(self, view) -> None:
        """Show another view in this row, or the same view after an edit.

        A re-list reuses the row widgets in place (see LoadViewCard.set_views),
        so the row must be able to change what it shows without being rebuilt.
        """
        self.view = view
        self.view_id = int(view.view_id)
        self.key_cell.set_slot(view.slot)
        self.name_label.setText(view.name)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        font = QFont(self.name_label.font())
        font.setWeight(QFont.Weight.Medium if selected else QFont.Weight.Normal)
        self.name_label.setFont(font)
        self.update()

    @property
    def selected(self) -> bool:
        return self._selected

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.pressed.emit(self.view_id)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.rename_asked.emit(self.view_id)
        event.accept()

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        pal = self.palette()
        if self._selected:
            painter.fillRect(self.rect(), palette.toward_text(pal, 0.10))
            painter.fillRect(0, 0, 2, self.height(), pal.color(QPalette.Highlight))
        elif self.underMouse():
            painter.fillRect(self.rect(), palette.toward_text(pal, 0.05))
        painter.fillRect(0, self.height() - 1, self.width(), 1, palette.toward_text(pal, 0.10))


def _flow_area(spacing: int = 4) -> tuple[QScrollArea, FlowLayout]:
    """A scrolling region holding a wrapping row of chips."""
    area = QScrollArea()
    area.setFrameShape(QFrame.NoFrame)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    area.setStyleSheet("QScrollArea { background: transparent; }")
    area.viewport().setAutoFillBackground(False)
    inner = QWidget()
    inner.setAutoFillBackground(False)
    flow = FlowLayout(inner, fill_min_width=0)
    flow.setContentsMargins(0, 0, 0, 0)
    flow.setSpacing(spacing)
    area.setWidget(inner)
    return area, flow


def _pills_in(flow) -> list[ChipPill]:
    widgets = [flow.itemAt(i).widget() for i in range(flow.count())]
    return [w for w in widgets if isinstance(w, ChipPill)]


def _clear_layout(layout) -> None:
    """Empty a layout of its widgets, hiding each until deleteLater takes it.

    Hidden in place rather than reparented to None: setParent(None) on a
    visible child makes it a top-level widget, the round trip of focus that
    closed a card in the omnibox chip incident (see _ChipWidget.prefix_label
    in omnibox.py), and it is the slower path besides -- 3.1 ms per 24 pills
    against 0.16 ms for hide and deleteLater, measured offscreen on
    2026-09-05.
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.deleteLater()


def _ghost_button(text: str, font: QFont, pal) -> QToolButton:
    """A borderless text button that shows a wash only under the mouse.

    The header's Rename and Delete, which must not compete with the name.
    """
    button = QToolButton()
    button.setText(text)
    button.setFont(font)
    button.setFixedHeight(24)
    button.setAutoRaise(True)
    button.setCursor(Qt.PointingHandCursor)
    button.setFocusPolicy(Qt.NoFocus)
    button.setStyleSheet(
        "QToolButton { border: none; border-radius: 2px; padding: 0 6px; }"
        f" QToolButton:hover {{ background: {_hex(palette.toward_text(pal, 0.08))}; }}"
    )
    return button


def _rule(pal) -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.NoFrame)
    line.setFixedHeight(1)
    line.setAutoFillBackground(True)
    rule_pal = line.palette()
    rule_pal.setColor(QPalette.Window, palette.track_colour(pal))
    line.setPalette(rule_pal)
    return line


class LoadViewCard(FilterCard):
    """The popover behind ``load:``, the Load pill and Ctrl+L."""

    KINDS = ()
    TITLE = "Saved views"
    WIDTH = CARD_WIDTH
    SHOWS_COUNT = False

    load_requested = Signal(int)          # view_id, from Load view
    slot_requested = Signal(int, object)  # view_id, slot or None
    rename_requested = Signal(int, str)   # view_id, the new name
    delete_requested = Signal(int)        # view_id
    import_requested = Signal(str, str)   # filter line, name ("" asks the store to suggest)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        pal = self.palette()
        small = caption_font(self.font())
        self._views: list = []
        self._taken: dict[int, str] = {}
        self._current_line = ""
        self._renaming = False
        self._renaming_id: int | None = None
        # The card itself is what holds the keyboard: the list takes no
        # focus (see the module docstring), so the arrows and digits arrive
        # here whichever row is selected -- or none.
        self.setFocusPolicy(Qt.StrongFocus)
        self.done_btn.setText("Load view")
        self.done_btn.setToolTip("Load the selected view")
        # The one slot menu every row's key cell opens (see _KeyMenu).
        self.key_menu = _KeyMenu(self)
        self.key_menu.chosen.connect(self._on_key_menu_chosen)

        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(0)
        self.body_layout.addWidget(_rule(pal))

        panes = QWidget()
        panes.setFixedHeight(BODY_HEIGHT)
        split = QHBoxLayout(panes)
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(0)
        self.body_layout.addWidget(panes)

        # ------------------------------------------------------ list column
        column = QFrame()
        column.setObjectName("viewcolumn")
        column.setFixedWidth(LIST_WIDTH)
        column.setStyleSheet(
            f"#viewcolumn {{ border-right: 1px solid {_hex(palette.track_colour(pal))}; }}"
        )
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(0)
        split.addWidget(column)

        self.view_list = QListWidget()
        self.view_list.setFocusPolicy(Qt.NoFocus)
        self.view_list.setFrameShape(QFrame.NoFrame)
        self.view_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view_list.setSelectionMode(QListWidget.SingleSelection)
        self.view_list.setUniformItemSizes(True)
        # The rows paint their own washes; the list must paint none of its
        # own under them or the two would stack.
        self.view_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: 0; }"
            " QListWidget::item { border: none; padding: 0; background: transparent; }"
            " QListWidget::item:selected, QListWidget::item:hover { background: transparent; }"
        )
        self.view_list.viewport().setAutoFillBackground(False)
        self.view_list.currentItemChanged.connect(lambda *_: self._on_selection_changed())
        column_layout.addWidget(self.view_list, 1)

        add_block = QFrame()
        add_block.setObjectName("addblock")
        add_block.setStyleSheet(
            f"#addblock {{ border-top: 1px solid {_hex(palette.track_colour(pal))}; }}"
        )
        add_layout = QVBoxLayout(add_block)
        add_layout.setContentsMargins(10, 8, 10, 8)
        add_layout.setSpacing(6)
        self.paste_edit = QLineEdit()
        self.paste_edit.setFixedHeight(26)
        self.paste_edit.setPlaceholderText(PASTE_PLACEHOLDER)
        self.paste_edit.textEdited.connect(self._on_paste_edited)
        add_layout.addWidget(self.paste_edit)
        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.import_name_edit = QLineEdit()
        self.import_name_edit.setFixedHeight(26)
        self.import_name_edit.setPlaceholderText(NAME_PLACEHOLDER)
        add_row.addWidget(self.import_name_edit, 1)
        self.add_btn = QPushButton("Add")
        self.add_btn.setEnabled(False)
        self.add_btn.setFocusPolicy(Qt.NoFocus)
        self.add_btn.clicked.connect(self._on_add)
        add_row.addWidget(self.add_btn)
        add_layout.addLayout(add_row)
        column_layout.addWidget(add_block)

        # ---------------------------------------------------- preview pane
        self.pane = QStackedWidget()
        split.addWidget(self.pane, 1)

        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        self.empty_label = QLabel(EMPTY_TEXT)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        empty_layout.addWidget(self.empty_label)
        self.pane.addWidget(empty)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(12, 10, 12, 8)
        detail_layout.setSpacing(8)
        self.pane.addWidget(detail)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.keycap = QLabel(NO_SLOT)
        self.keycap.setObjectName("keycap")
        self.keycap.setAlignment(Qt.AlignCenter)
        self.keycap.setFixedHeight(26)
        self.keycap.setMinimumWidth(26)
        cap_font = QFont(self.keycap.font())
        cap_font.setWeight(QFont.Weight.DemiBold)
        self.keycap.setFont(cap_font)
        header.addWidget(self.keycap)
        self.name_label = _ElidedLabel("")
        name_font = QFont(self.name_label.font())
        name_font.setWeight(QFont.Weight.DemiBold)
        self.name_label.setFont(name_font)
        header.addWidget(self.name_label, 1)
        self.name_edit = QLineEdit()
        self.name_edit.setFixedHeight(24)
        self.name_edit.setVisible(False)
        header.addWidget(self.name_edit, 1)
        self.rename_btn = _ghost_button("Rename", small, pal)
        self.rename_btn.clicked.connect(self.begin_rename)
        header.addWidget(self.rename_btn)
        self.delete_btn = _ghost_button("Delete", small, pal)
        danger = palette.status_hex(palette.CRITICAL, pal)
        self.delete_btn.setStyleSheet(
            self.delete_btn.styleSheet() + f" QToolButton {{ color: {danger}; }}"
        )
        self.delete_btn.setToolTip("Forget this view and free its key")
        self.delete_btn.clicked.connect(self._on_delete)
        header.addWidget(self.delete_btn)
        detail_layout.addLayout(header)

        # The view's own chips take the top half of the pane and the diff the
        # bottom half, whatever either holds: a region that hugged its content
        # left a saved view with many filters three visible lines and a
        # scrollbar too thin to notice, with empty pane under it.
        self.chips_area, self.chips_flow = _flow_area()
        detail_layout.addWidget(self.chips_area, 1)
        # What a load does beyond the filter, in the caption style the Save
        # card used before it grew controls; hidden for a view that records
        # no posture, since "leaves everything alone" needs no line.
        self.posture_label = QLabel("")
        self.posture_label.setFont(small)
        self.posture_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.posture_label.setVisible(False)
        detail_layout.addWidget(self.posture_label)

        detail_layout.addWidget(_rule(pal))
        self.diff_caption = QLabel(DIFF_CAPTION)
        self.diff_caption.setFont(small)
        self.diff_caption.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        detail_layout.addWidget(self.diff_caption)
        self.diff_area, self.diff_flow = _flow_area()
        detail_layout.addWidget(self.diff_area, 1)
        # Lives inside the diff region so the bottom half keeps its height
        # when there is nothing to show but the one line.
        self.identical_label = QLabel(IDENTICAL_TEXT)
        self.identical_label.setFont(small)
        self.identical_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.identical_label.setVisible(False)

        self.hint_label = QLabel("")
        self.hint_label.setFont(small)
        self.hint_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        detail_layout.addWidget(self.hint_label)

        # Filters go on last: adding an edit to a layout already sends it
        # events, and the filter names widgets built after the first edit.
        for edit in (self.paste_edit, self.import_name_edit, self.name_edit):
            edit.installEventFilter(self)
        self._style_keycap(None)
        self._update_done_enabled()

    # ------------------------------------------------------------------- API
    def set_views(self, views_: list, selected_view_id: int | None = None) -> None:
        """Fill the list, selecting one view by id.

        With no id given, whichever view is selected now stays selected: a
        re-list after a rename or a slot change must not move the selection
        the user made.

        A list of the same views is refreshed in the row widgets it already
        has, position by position: a slot move or a rename reorders the
        library (slotted first, then by name) but rebuilding every row for
        it cost a 200-view library as much as opening the card did. The
        rows are torn down and built afresh only when the set of views
        changed -- an import or a delete.
        """
        wanted = selected_view_id
        if wanted is None:
            current = self.selected_view()
            if current is not None:
                wanted = int(current.view_id)
        self._end_rename(commit=False)
        self._views = list(views_)
        self._taken = {int(v.slot): v.name for v in self._views if v.slot is not None}
        same_set = {int(v.view_id) for v in self._views} == {
            int(self.view_list.item(i).data(Qt.UserRole)) for i in range(self.view_list.count())
        } and len(self._views) == self.view_list.count()
        self.view_list.blockSignals(True)
        try:
            if same_set:
                for index, view in enumerate(self._views):
                    item = self.view_list.item(index)
                    item.setData(Qt.UserRole, int(view.view_id))
                    item.setData(Qt.UserRole + 1, view)
                    row = self.view_list.itemWidget(item)
                    row.set_view(view)
                    row.key_cell.set_taken(self._taken)
            else:
                self.view_list.clear()
                for view in self._views:
                    item = QListWidgetItem()
                    item.setData(Qt.UserRole, int(view.view_id))
                    item.setData(Qt.UserRole + 1, view)
                    item.setSizeHint(QSize(LIST_WIDTH - 1, ROW_HEIGHT))
                    row = _ViewRow(view, menu=self.key_menu)
                    row.key_cell.set_taken(self._taken)
                    row.pressed.connect(self._select_view)
                    row.rename_asked.connect(self._on_row_double_clicked)
                    row.slot_chosen.connect(self.slot_requested)
                    self.view_list.addItem(item)
                    self.view_list.setItemWidget(item, row)
            self._select_view(wanted)
        finally:
            self.view_list.blockSignals(False)
        self._on_selection_changed()

    def set_current(self, state) -> None:
        """The live omnibox filter, which the preview diffs the selected view against.

        A load is not additive, and the chips it would throw away are worth
        seeing before Load view.
        """
        self._current_line = state.filter.strip()
        self._refresh_pane()

    def selected_view(self):
        """The selected views.View, or None with nothing selected."""
        item = self.view_list.currentItem()
        return None if item is None else item.data(Qt.UserRole + 1)

    def diff(self) -> list[tuple[str, object]]:
        """The (sign, token) pairs the pane shows for the selected view."""
        view = self.selected_view()
        if view is None:
            return []
        return views.diff_tokens(self._current_line, view.state.filter)

    def preview_pills(self) -> list[ChipPill]:
        """The selected view's chips as shown.

        None for an unfiltered view, whose region carries a caption instead.
        """
        return _pills_in(self.chips_flow)

    def diff_pills(self) -> list[ChipPill]:
        return _pills_in(self.diff_flow)

    def rows(self) -> list[_ViewRow]:
        return [
            self.view_list.itemWidget(self.view_list.item(i))
            for i in range(self.view_list.count())
        ]

    # ----------------------------------------------------------- renaming
    def begin_rename(self) -> None:
        """Swap the header's name for an edit.

        Enter, leaving the field and selecting another row all commit, Esc
        abandons; either way the label comes back. The commit names the
        view the rename began on, not whichever row is selected when it
        lands: a click on another row commits the old row's name and then
        selects the new one.
        """
        view = self.selected_view()
        if view is None:
            return
        self._renaming = True
        self._renaming_id = int(view.view_id)
        self.name_edit.setText(view.name)
        self.name_label.setVisible(False)
        self.rename_btn.setVisible(False)
        self.name_edit.setVisible(True)
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    @property
    def renaming(self) -> bool:
        return self._renaming

    def _end_rename(self, *, commit: bool) -> None:
        # The flag drops first: hiding the edit takes its focus, and that
        # FocusOut must not arrive here as a second commit.
        if not self._renaming:
            return
        self._renaming = False
        name = self.name_edit.text().strip()
        view = next((v for v in self._views if int(v.view_id) == self._renaming_id), None)
        self._renaming_id = None
        self.name_edit.setVisible(False)
        self.name_label.setVisible(True)
        self.rename_btn.setVisible(True)
        self.setFocus()
        # A blank field is a change of mind rather than a rename to nothing,
        # and the unchanged name is not a rename at all.
        if commit and view is not None and name and name != view.name:
            self.rename_requested.emit(int(view.view_id), name)

    # ------------------------------------------------------ subclass contract
    def seed(self, chips: list) -> None:
        """Start over: no selection, an empty paste row, and no half-finished rename.

        No selection so Load view cannot load a view nobody picked.
        """
        self._end_rename(commit=False)
        self.paste_edit.clear()
        self.import_name_edit.clear()
        self.import_name_edit.setPlaceholderText(NAME_PLACEHOLDER)
        self.view_list.setCurrentItem(None)
        self._on_selection_changed()
        self._update_add_enabled()

    def chips(self) -> list:
        """Nothing: a load arrives through the view's set_spec, not chip replacement."""
        return []

    def owns(self, chip) -> bool:
        return False

    def totals(self) -> tuple[str, object]:
        return "none", None

    def first_focus(self) -> QWidget:
        """The card itself, always.

        The arrows and the digits are handled here, Enter is swallowed, and
        the list takes no focus at all -- so no visit can open on a row the
        user never picked.
        """
        return self

    def apply(self) -> None:
        """Load view: load the selected view.

        ``done`` is never emitted -- the shell's signal replaces chips of the
        card's kinds, and a whole view replaces the filter instead.
        """
        view = self.selected_view()
        if view is None:
            return
        self._applied = True
        self.hide()
        self.load_requested.emit(int(view.view_id))

    # ------------------------------------------------------------- internals
    def _select_view(self, view_id: int | None) -> None:
        for index in range(self.view_list.count()):
            item = self.view_list.item(index)
            if view_id is not None and int(item.data(Qt.UserRole)) == view_id:
                self.view_list.setCurrentItem(item)
                self.view_list.scrollToItem(item)
                return
        self.view_list.setCurrentItem(None)

    def _move_selection(self, step: int) -> None:
        count = self.view_list.count()
        if count == 0:
            return
        current = self.view_list.currentRow()
        if current < 0:
            target = 0 if step > 0 else count - 1
        else:
            target = min(max(current + step, 0), count - 1)
        self.view_list.setCurrentRow(target)
        self.view_list.scrollToItem(self.view_list.item(target))

    def _on_selection_changed(self) -> None:
        # Moving to another row commits a rename in progress, the rule the
        # field's own blur follows: a name typed and then clicked away from
        # was meant. The commit goes to the row the rename began on (see
        # begin_rename), and a re-list it triggers lands re-entrantly here
        # with nothing left to commit.
        self._end_rename(commit=True)
        selected = self.selected_view()
        for row in self.rows():
            row.set_selected(selected is not None and row.view_id == int(selected.view_id))
        self._refresh_pane()
        self._update_done_enabled()

    def _on_row_double_clicked(self, view_id: int) -> None:
        self._select_view(view_id)
        self.begin_rename()

    def _on_delete(self) -> None:
        view = self.selected_view()
        if view is not None:
            self.delete_requested.emit(int(view.view_id))

    def _on_key_menu_chosen(self, cell, slot: int | None) -> None:
        # The shared menu names the cell that opened it; the cell's own rule
        # (the digit it already holds is not a request) still applies.
        if cell is not None:
            cell.pick(slot)

    def _refresh_pane(self) -> None:
        view = self.selected_view()
        # The identical line is the one child of the diff flow that must
        # survive a rebuild: _clear_layout deletes everything it finds, so
        # the label is lifted out and hidden first.
        self.identical_label.setVisible(False)
        self.diff_flow.removeWidget(self.identical_label)
        self.identical_label.setParent(self)
        _clear_layout(self.chips_flow)
        _clear_layout(self.diff_flow)
        if view is None:
            self.pane.setCurrentIndex(0)
            return
        self.pane.setCurrentIndex(1)
        self._style_keycap(view.slot)
        self.name_label.setText(view.name)
        posture = describe_posture(view.state)
        self.posture_label.setText(posture)
        self.posture_label.setVisible(bool(posture))
        spec = omni.parse(view.state.filter)
        tokens = [*spec.chips, *spec.text.split()]
        if tokens:
            for token in tokens:
                self.chips_flow.addWidget(
                    ChipPill(token, max_width=PANE_CHIP_WIDTH, parent=self.chips_area.widget())
                )
        else:
            unfiltered = QLabel(views.UNFILTERED, self.chips_area.widget())
            unfiltered.setFont(caption_font(self.font()))
            unfiltered.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
            self.chips_flow.addWidget(unfiltered)
        diff = self.diff()
        identical = all(sign == "=" for sign, _token in diff)
        if identical:
            # Re-homed each rebuild: _refresh_pane lifted it out before the
            # flow was cleared, so it is the one child that survives.
            self.identical_label.setParent(self.diff_area.widget())
            self.diff_flow.addWidget(self.identical_label)
            self.identical_label.setVisible(True)
        else:
            for sign, token in diff:
                self.diff_flow.addWidget(
                    ChipPill(
                        token, sign=sign, max_width=PANE_CHIP_WIDTH,
                        parent=self.diff_area.widget(),
                    )
                )
        self.hint_label.setText(
            f"Press Ctrl+{view.slot} to load" if view.slot is not None
            else "Assign a key for one-press access, or use Load view"
        )

    def _style_keycap(self, slot: int | None) -> None:
        pal = self.palette()
        edge = _hex(palette.track_colour(pal))
        style = "dashed" if slot is None else "solid"
        self.keycap.setText(NO_SLOT if slot is None else str(slot))
        self.keycap.setStyleSheet(
            f"#keycap {{ border: 1px solid {edge}; border-bottom: 2px {style} {edge};"
            " border-radius: 2px; padding: 0 6px;"
            f" color: {palette.SECONDARY_TEXT if slot is None else 'palette(window-text)'}; }}"
        )

    def _on_paste_edited(self, text: str) -> None:
        # The placeholder says what a blank name saves as, so the user can
        # leave it: the store suggests the same name from the same line.
        suggested = views.suggest_name(omni.parse(text)) if text.strip() else ""
        self.import_name_edit.setPlaceholderText(suggested or NAME_PLACEHOLDER)
        self._update_add_enabled()

    def _on_add(self) -> None:
        if self.add_btn.isEnabled():
            self.import_requested.emit(
                self.paste_edit.text(), self.import_name_edit.text().strip()
            )

    def _update_add_enabled(self) -> None:
        self.add_btn.setEnabled(bool(self.paste_edit.text().strip()))

    def _update_done_enabled(self) -> None:
        self.done_btn.setEnabled(self.selected_view() is not None)

    # ---------------------------------------------------------------- events
    def event(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.ShortcutOverride and event.key() in _DIGIT_KEYS:
            # The tab binds Ctrl+digit to load a view from anywhere, with a
            # WidgetWithChildrenShortcut context -- and Qt's context check
            # walks up through a Qt.Popup to its parent, so with the card
            # up the tab's shortcut would win the key: the view loaded but
            # the card stayed open over it with a diff against the filter
            # that was. Claiming the override keeps the press for the
            # card's keyPressEvent, which selects, loads and closes. The
            # override climbs here from the card's edits too, so Ctrl+digit
            # in the paste box loads as well, while a bare digit typed
            # there is still text (the edit accepts the key press itself).
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key_Up, Qt.Key_Down):
            self._move_selection(-1 if key == Qt.Key_Up else 1)
            event.accept()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            # Enter never loads here. The shell's rule (Enter is Done) suits
            # a card whose Done writes chips; loading replaces the whole
            # filter, group-by and rail, and a key that also commits renames
            # and paste imports elsewhere in this card is too easy to land
            # on the wrong thing. The digit and the Load view button remain.
            event.accept()
            return
        if key in _DIGIT_KEYS:
            # The digit is the one-press path, with or without Ctrl (the
            # tab-wide shortcut is Ctrl+digit, and a hand that has just used
            # it should not have to learn a second spelling here): select
            # the view on that key and load it. An empty key does nothing
            # rather than loading whatever is selected.
            slot = _DIGIT_KEYS[key]
            view = next((v for v in self._views if v.slot == slot), None)
            if view is not None:
                self._select_view(int(view.view_id))
                self.apply()
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.name_edit:
            if event.type() == QEvent.KeyPress:
                if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                    # Enter renames and stops here: left to travel it would
                    # climb to the card and load the selected row, which is
                    # not what someone correcting a name asked for.
                    self._end_rename(commit=True)
                    return True
                if event.key() == Qt.Key_Escape:
                    # Likewise Esc: it abandons the rename, not the card.
                    self._end_rename(commit=False)
                    return True
            elif event.type() == QEvent.FocusOut and self._renaming:
                self._end_rename(commit=True)
        if (
            obj in (self.paste_edit, self.import_name_edit)
            and event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Return, Qt.Key_Enter)
        ):
            # Enter in the add block means "add it", never Load view: a
            # QLineEdit ignores the key, so left to travel it would climb to
            # the card and load a selected view while the user was naming an
            # import (the fit card's name field, same reason).
            self._on_add()
            return True
        return super().eventFilter(obj, event)
