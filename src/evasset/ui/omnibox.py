"""The omnibox: one bordered field that owns the whole filter state.

The previous Assets tab spread its filter state across a search box, three
combo boxes, two checkboxes and a one-slot label from the group panel. Any of
them could be narrowing the table while scrolled out of sight, and undoing a
filter meant remembering which control held it. Chips fix that structurally:
every active constraint is a small labelled widget sitting inside the search
field itself, so the filter state is never hidden and each piece is deletable
exactly where it is shown -- the cross on the chip, not a Clear button
somewhere else. Typed tokens (``cat:Mineral``, ``-owner:Alt``) migrate into
chips the moment they are committed, so text in the field is always and only
the free-text part of the filter.

Escape deliberately ladders instead of doing one thing. It first clears the
text being typed, then pops the newest chip, and only once the field is
completely empty does the escape_pressed signal fire (which the view uses to
close panels). Each press undoes the most recent, cheapest layer of state, so
mashing escape always walks back to an unfiltered table without skipping past
anything -- the alternative, one escape clearing everything at once, throws
away a carefully built chip set because the user only meant to abandon a
half-typed word.

Completion looks up real values for the token being typed -- ``cat:min``
offers the categories matching "min", each with its stack count -- through
one grouped COUNT query per keystroke, run via AsyncQuery so a slow lookup
can neither freeze typing nor land out of order (the generation guard and
QRunnable lifetime rules are documented in async_query.py). ``is:`` and
``val:`` tokens get no lookup on purpose: the flag vocabulary is a handful of
fixed words and ``val:`` is a comparison the user writes, so the database has
nothing to offer for either. ``stat:`` sits between the two: the attribute
name half of its value completes from the SDE's mutable dogma attributes (and
the curated aliases in abyssal.STAT_ALIASES), while the operator and number
stay the user's to type -- so picking a completion fills the name in and
leaves the field open rather than minting a chip the grammar would reject.
``roll:`` shares that completion: it names the same attributes, only ranked
by roll quality instead of compared by value.

The ``abyssal`` chip is the one chip with a second button. Its value is a
list of module types, which no single-line completer builds well, and the
stat rows that go with it (one slider per rolled attribute) do not fit in a
chip at all -- so the chip carries a glyph that asks the owning view, via
card_requested, to open the complex-search card anchored under it. The same
request goes out on its own when the chip is minted by typing (Enter, or the
draft builder), one event turn later; see _request_card_later. The chip
itself renders as "Abyssal", "Abyssal · Stasis Webifier" or "Abyssal · 3
types" with its prefix hidden: the word is the kind, and repeating it as a
muted ``abyssal:`` in front would say the same thing twice.
"""

from __future__ import annotations

import re
import sqlite3

from PySide6.QtCore import QEvent, QModelIndex, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from .. import abyssal, omni, queries
from . import palette
from .async_query import AsyncQuery
from .debounce import Debounce
from .flow_layout import FlowLayout

# The prefixes users type, mapped to the canonical Chip kinds omni.parse
# produces -- and back again for rendering, so a chip displays the short form
# a user would have typed to create it. A deliberate subset of omni.py's own
# parse table: only the canonical short spellings drive completion and
# rendering here, while the long forms (location:, system:, category:) still
# parse once committed.
_KIND_FOR_PREFIX = {
    "loc": "location",
    "sys": "system",
    "region": "region",
    "owner": "owner",
    "cat": "category",
    "group": "group",
    "meta": "meta",
    # item completes like any level kind -- without this entry a typed
    # "item:Dom" silently offered nothing while the draft builder completed
    # the very same values.
    "item": "item",
    "stat": omni.STAT_KIND,
    "roll": omni.ROLL_KIND,
    "abyssal": omni.ABYSSAL_KIND,
}
_PREFIX_FOR_KIND = {kind: prefix for prefix, kind in _KIND_FOR_PREFIX.items()}

# The two kinds whose value opens with an attribute name and closes with a
# comparison the user types; they share one completion and one commit path.
_ATTRIBUTE_KINDS = (omni.STAT_KIND, omni.ROLL_KIND)

# Output column of queries.ASSET_ROWS per completable kind. Only kinds listed
# here get a completion popup at all.
_COMPLETION_COLUMN = {
    "location": "location",
    "system": "system",
    "region": "region",
    "owner": "owner",
    "category": "category",
    "group": "grp",
    "meta": "meta",
    "item": "item",
}

_TOKEN_RE = re.compile(r"^(-?)([A-Za-z]+):(.*)$")

# Every kind the draft-chip builder offers, in the order its list shows them.
_ALL_KINDS = (
    *omni.LEVEL_KINDS, "item", "is", "val", omni.STAT_KIND, omni.ROLL_KIND, omni.ABYSSAL_KIND,
)

# Placeholder per chosen kind: the draft's value stage should say what kind
# of thing it wants, because "value…" teaches nothing for the kinds whose
# vocabulary is fixed or whose value has a shape to learn.
_VALUE_PLACEHOLDER = {
    "is": " / ".join(omni.IS_FLAGS),
    "val": ">10m   <1b   >=500k …",
    omni.STAT_KIND: '"CPU usage"<30   web>55   duration<9',
    omni.ROLL_KIND: "web>=70   cpu=60..90",
    omni.ABYSSAL_KIND: "module type, or Enter for every abyssal item",
}

# Abyssal type names the user owns, for the abyssal chip's value stage: the
# same COUNT projection the level kinds use, restricted to dynamic types.
_ABYSSAL_TYPES_SQL = (
    f"SELECT item AS label, COUNT(*) AS stacks FROM ({queries.ASSET_ROWS})"
    " WHERE is_dynamic_type = 1 AND label LIKE ? ESCAPE '\\'"
    " GROUP BY label ORDER BY stacks DESC, label COLLATE NOCASE LIMIT 12"
)

# Attribute names a stat: value can start with: every dogma attribute some
# mutaplasmid rolls, matched on the English display name or CCP's internal
# name. One row per attribute (DISTINCT, because one attribute appears in
# many mutators' ranges) rather than per display name, with a count of how
# many mutable attributes share the display name: in the real SDE 554
# signatureRadiusBonus (%) and 983 signatureRadiusAdd (m) both display
# "Signature Radius Modifier", and a completion that wrote the shared
# display name back would mint a chip matching either. _stat_option offers
# the internal name for those, annotated with the display name and unit.
_STAT_NAMES_SQL = """
WITH mutable AS (
    SELECT DISTINCT d.attribute_id, d.name, d.display_name, u.display_name AS unit
    FROM sde_dogma_attributes d
    JOIN sde_mutator_ranges m ON m.attribute_id = d.attribute_id
    LEFT JOIN sde_dogma_units u ON u.unit_id = d.unit_id
    WHERE d.display_name IS NOT NULL AND d.display_name <> ''
)
SELECT name, display_name, unit,
       (SELECT COUNT(*) FROM mutable x WHERE x.display_name = mutable.display_name) AS shared
FROM mutable
WHERE display_name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\'
ORDER BY display_name COLLATE NOCASE, name LIMIT 12
"""
# The same shape for one alias target, so an alias to a shared display name
# ("sig" -> signatureRadiusBonus) is offered as the internal name too.
_STAT_ALIAS_SQL = """
SELECT d.name, d.display_name, u.display_name AS unit,
       (SELECT COUNT(DISTINCT x.attribute_id)
        FROM sde_dogma_attributes x
        JOIN sde_mutator_ranges mx ON mx.attribute_id = x.attribute_id
        WHERE x.display_name = d.display_name) AS shared
FROM sde_dogma_attributes d
LEFT JOIN sde_dogma_units u ON u.unit_id = d.unit_id
WHERE d.name = ?
"""

_VALUE_ROLE = Qt.UserRole
_KIND_ROLE = Qt.UserRole + 1
_NEGATED_ROLE = Qt.UserRole + 2


def _split_trailing_token(text: str) -> tuple[str, str]:
    """Head of the field, and the token still being typed after the last
    space. A quoted value mid-entry can legitimately contain spaces;
    completion simply stops offering once one appears, which costs little --
    the popup exists for prefix narrowing, and a user deep inside a quoted
    station name has already chosen their value."""
    if " " not in text:
        return "", text
    head, _space, tail = text.rpartition(" ")
    return head + " ", tail


def _like_pattern(fragment: str) -> str:
    """Substring LIKE with any user-typed wildcard characters neutralised, so
    a stray % in the field cannot turn a narrowing lookup into a full list."""
    escaped = fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _quote(value: str) -> str:
    """Quote a value for the field the way to_text() would, so the token
    written back by a stat completion parses to exactly that name."""
    if value and '"' not in value and not any(ch.isspace() for ch in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _option_text(row) -> str:
    """One completion line: the label, its stack count when the lookup has
    one, and the alias that led here when it was an alias match."""
    text = str(row["label"])
    stacks = row["stacks"]
    if stacks is not None:
        text += f" · {stacks:,}"
    note = row["note"] if "note" in row.keys() else None
    if note:
        text += f"  ({note})"
    return text


def _completion_fetch(kind: str, fragment: str):
    """The pool-thread lookup behind a completion popup, or None when the
    kind has nothing to offer (val:, or a kind with no column)."""
    if kind in _ATTRIBUTE_KINDS:
        return _stat_completion_fetch(fragment)
    if kind == omni.ABYSSAL_KIND:
        pattern = _like_pattern(fragment)

        def fetch_types(conn: sqlite3.Connection) -> list:
            return list(conn.execute(_ABYSSAL_TYPES_SQL, (pattern,)))

        return fetch_types
    column = _COMPLETION_COLUMN.get(kind)
    if column is None:
        return None
    # A projection of queries.ASSET_ROWS: building on the exact same SELECT
    # guarantees the labels offered are precisely the strings the resulting
    # chip will filter on. Its shape (LIKE, count ordering, LIMIT) is a
    # property of this popup, not a reusable read, so it lives beside the
    # completer rather than in queries.py.
    sql = (
        f"SELECT {column} AS label, COUNT(*) AS stacks"
        f" FROM ({queries.ASSET_ROWS})"
        " WHERE label IS NOT NULL AND label LIKE ? ESCAPE '\\'"
        " GROUP BY label ORDER BY stacks DESC, label COLLATE NOCASE LIMIT 12"
    )
    pattern = _like_pattern(fragment)

    def fetch(conn: sqlite3.Connection) -> list:
        return list(conn.execute(sql, (pattern,)))

    return fetch


def _stat_option(row, alias: str | None = None) -> dict:
    """One stat: completion from a _STAT_NAMES_SQL or _STAT_ALIAS_SQL row.

    The label is what gets written into the field, so it must be a name the
    grammar resolves to exactly this attribute: the display name when it is
    unique among mutable attributes, the internal name when it is shared,
    with the display name and unit as the annotation so the two namesakes
    can be told apart in the popup. An alias that led here is shown too.
    """
    if row["shared"] > 1:
        label = row["name"]
        note = f"{row['display_name']}, {row['unit']}" if row["unit"] else row["display_name"]
        if alias:
            note = f"{alias} · {note}"
    else:
        label, note = row["display_name"], alias
    return {"label": label, "stacks": None, "note": note}


def _stat_completion_fetch(fragment: str):
    """Attribute names for a stat: value. Once an operator has been typed
    the name is settled, so there is nothing left to offer."""
    if "<" in fragment or ">" in fragment:
        return None
    needle = fragment.strip().lower()
    pattern = _like_pattern(fragment.strip())

    def fetch(conn: sqlite3.Connection) -> list[dict]:
        rows = [_stat_option(r) for r in conn.execute(_STAT_NAMES_SQL, (pattern, pattern))]
        if not needle:
            return rows
        # Aliases ride on top of the name matches, resolved to the canonical
        # name so the chip carries one the grammar will match, and shown
        # with the alias beside them so the user sees what "cpu" meant. An
        # alias row replaces the plain row for the same name rather than
        # sitting next to a duplicate of it.
        aliased: list[dict] = []
        for alias, internal in sorted(abyssal.STAT_ALIASES.items()):
            if not alias.startswith(needle):
                continue
            hit = conn.execute(_STAT_ALIAS_SQL, (internal,)).fetchone()
            if hit is None or not hit["display_name"]:
                continue
            option = _stat_option(hit, alias)
            if option["label"] not in {o["label"] for o in aliased}:
                aliased.append(option)
        taken = {o["label"] for o in aliased}
        return (aliased + [r for r in rows if r["label"] not in taken])[:12]

    return fetch


def abyssal_chip_label(chip: omni.Chip) -> str:
    """The abyssal chip's text: the word alone for every dynamic type, the
    one type with its "Abyssal " prefix stripped (the chip already says it),
    or a count -- three long type names would eat the whole field."""
    types = omni.split_types(chip.value)
    if not types:
        return "Abyssal"
    if len(types) == 1:
        return f"Abyssal · {abyssal.strip_type_prefix(types[0])}"
    return f"Abyssal · {len(types)} types"


class _ChipWidget(QFrame):
    """One removable filter token: muted ``kind:`` prefix, the value, and a
    cross. The prefix is muted because the value is the payload -- the kind is
    scaffolding the eye should be able to skip once the colour has already
    said which axis this chip filters (each kind wears its own wash) and, in
    red, whether it excludes rather than includes.

    The abyssal chip alone hides its prefix (its label already is the kind)
    and grows card_btn, the glyph that opens the complex-search card; on
    every other chip card_btn is None so callers can tell the two apart
    without knowing the kind."""

    def __init__(self, chip: omni.Chip, parent: QWidget | None = None):
        super().__init__(parent)
        wash = palette.chip_tint(chip.kind, chip.negated)
        self.setObjectName("chip")
        self.setStyleSheet(f"#chip {{ background: {wash}; border-radius: 9px; }}")

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 2, 3, 2)
        row.setSpacing(2)

        is_abyssal = chip.kind == omni.ABYSSAL_KIND
        prefix = ("-" if chip.negated else "") + _PREFIX_FOR_KIND.get(chip.kind, chip.kind) + ":"
        if is_abyssal:
            # A negated abyssal chip keeps only the minus: the wash already
            # went red, and "-abyssal: Abyssal" would say the kind twice.
            prefix = "-" if chip.negated else ""
        self.prefix_label = QLabel(prefix)
        self.prefix_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.prefix_label.setVisible(bool(prefix))
        row.addWidget(self.prefix_label)

        self.value_label = QLabel(abyssal_chip_label(chip) if is_abyssal else chip.value)
        row.addWidget(self.value_label)

        self.card_btn: QToolButton | None = None
        if is_abyssal:
            self.card_btn = QToolButton()
            self.card_btn.setText("▾")
            self.card_btn.setAutoRaise(True)
            self.card_btn.setCursor(Qt.PointingHandCursor)
            self.card_btn.setToolTip("Refine: module type and stat ranges")
            self.card_btn.setStyleSheet("QToolButton { border: none; padding: 0 2px; }")
            row.addWidget(self.card_btn)

        self.close_btn = QToolButton()
        self.close_btn.setText("×")
        self.close_btn.setAutoRaise(True)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("Remove this filter")
        self.close_btn.setStyleSheet("QToolButton { border: none; padding: 0 2px; }")
        row.addWidget(self.close_btn)


class _PlusButton(QToolButton):
    """The draft-builder entrance: a translucent green tile with the plus cut
    out of it, so the glyph is the omnibox's own background showing through.

    Painted by hand because no stylesheet can express negative space. The
    tile is drawn into an alpha image first -- the bars are then *cleared*
    rather than painted, which is what makes them transparent -- and the
    image is composited onto the widget. Hover and press deepen the wash;
    that, not a label, is what says "this does something": a flat "+" read
    as decoration, and a "+ Filter" label was judged noise once the colour
    carried the message.
    """

    _TILE = 22

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # A 1 px gutter each side gives the rounded tile breathing room
        # against the chips and the line edit beside it.
        self.setFixedSize(self._TILE + 2, self._TILE)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Build a filter (Ctrl+F)")
        self.setAccessibleName("Build a filter")
        self.setAttribute(Qt.WA_Hover, True)
        self.setFocusPolicy(Qt.TabFocus)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        dpr = self.devicePixelRatioF()
        image = QImage(
            round(self.width() * dpr), round(self.height() * dpr), QImage.Format_ARGB32_Premultiplied
        )
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.transparent)

        pair = palette.POSITIVE
        green = QColor(pair[1] if palette.is_dark(self.palette()) else pair[0])
        # Translucent by design: the wash tints whatever the field paints
        # underneath, and the cut-out plus reads as the same surface.
        green.setAlphaF(0.62 if self.isDown() else 0.5 if self.underMouse() else 0.34)

        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(green)
        painter.drawRoundedRect(1, 0, self.width() - 2, self.height(), 6, 6)
        # Negative space: clearing to transparent is what makes the plus the
        # background rather than a lighter green.
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        cx, cy = self.width() / 2, self.height() / 2
        arm, thick = 5.0, 2.0
        painter.drawRect(QRectF(cx - arm, cy - thick / 2, arm * 2, thick))
        painter.drawRect(QRectF(cx - thick / 2, cy - arm, thick, arm * 2))
        painter.end()

        painter = QPainter(self)
        painter.drawImage(0, 0, image)
        if self.hasFocus():
            # Keyboard users still need to see where the focus is.
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QColor(green.red(), green.green(), green.blue()))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(1.5, 0.5, self.width() - 3, self.height() - 1), 6, 6)


class _DraftChip(QFrame):
    """A chip under construction: pick the kind by its first letters, then
    type the value. Ctrl+F or the + button opens one; Enter walks it
    forward, Escape abandons it, Backspace on an empty value steps back to
    the kind stage.

    The two-stage flow exists for discoverability. The token grammar is fast
    once known, but nothing about a bare text field teaches "cat:" -- an
    empty card that first lists what can be filtered and then completes real
    values is the grammar made visible. The card takes the kind's wash the
    moment one is chosen, the same lesson the finished chips teach; until
    then a dashed border says "draft"."""

    committed = Signal(object)  # omni.Chip
    cancelled = Signal()
    value_fragment_edited = Signal(str, str)  # kind, typed fragment

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind: str | None = None
        self.negated = False
        self.setObjectName("draftchip")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._restyle()

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 2, 8, 2)
        row.setSpacing(2)
        self.prefix_label = QLabel("")
        self.prefix_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.prefix_label.setVisible(False)
        row.addWidget(self.prefix_label)
        self.edit = QLineEdit()
        self.edit.setFrame(False)
        self.edit.setStyleSheet("background: transparent;")
        self.edit.setPlaceholderText("filter type…")
        self.edit.setFixedWidth(170)
        row.addWidget(self.edit)

        # Stage 1: the kinds themselves, prefix-filtered by QCompleter, so
        # "gro" narrows to group and "l" already means location.
        self._kind_completer = QCompleter(list(_ALL_KINDS), self)
        self._kind_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._kind_completer.setWidget(self.edit)
        # The whole point of the list is showing every axis at once; the
        # default of 7 visible items silently scrolled item/is/val away.
        self._kind_completer.setMaxVisibleItems(len(_ALL_KINDS))
        self._kind_completer.activated[str].connect(self._kind_picked)

        # Stage 2: real values with stack counts, fed by the owning Omnibox
        # (it owns the AsyncQuery; this card stays database-free).
        self._value_model = QStandardItemModel(self)
        self._value_completer = QCompleter(self)
        self._value_completer.setModel(self._value_model)
        self._value_completer.setWidget(self.edit)
        self._value_completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self._value_completer.setMaxVisibleItems(10)
        self._value_completer.activated[QModelIndex].connect(self._value_picked)

        self.edit.textEdited.connect(self._on_edited)
        self.edit.returnPressed.connect(self._on_return)
        self.edit.installEventFilter(self)

    # ------------------------------------------------------------- lifecycle
    def begin(self) -> None:
        self.edit.setFocus(Qt.ShortcutFocusReason)
        # The popup anchors to the edit's geometry at complete() time, and
        # this card was inserted into the row a moment ago -- the layout
        # pass that gives it a real position runs on the event loop, so the
        # popup must wait one turn or it opens against the parent's origin
        # and floats over the table.
        QTimer.singleShot(0, self._show_kind_popup)

    def _show_kind_popup(self) -> None:
        if self.kind is not None:
            return  # already advanced to the value stage
        self._kind_completer.setCompletionPrefix(self.edit.text().lstrip("-"))
        self._kind_completer.complete()

    def _restyle(self) -> None:
        if self.kind is None:
            self.setStyleSheet(
                "#draftchip { border: 1px dashed palette(shadow);"
                " border-radius: 9px; background: transparent; }"
            )
        else:
            wash = palette.chip_tint(self.kind, self.negated)
            self.setStyleSheet(
                f"#draftchip {{ background: {wash}; border-radius: 9px; }}"
            )

    # ------------------------------------------------------------ kind stage
    def _resolve_kind(self, raw: str) -> tuple[str, bool] | None:
        text = raw.strip()
        negated = text.startswith("-")
        text = text.lstrip("-").strip().lower()
        if not text:
            return None
        kind = _KIND_FOR_PREFIX.get(text)
        if kind is None:
            matches = [k for k in _ALL_KINDS if k.startswith(text)]
            kind = matches[0] if len(matches) == 1 else None
        return (kind, negated) if kind else None

    def _kind_picked(self, name: str) -> None:
        # Negation is typed before the kind ("-gro"), so it rides on the
        # field text rather than on the completer's suggestion.
        negated = self.edit.text().strip().startswith("-")
        self._enter_value_stage(name, negated)

    def _enter_value_stage(self, kind: str, negated: bool) -> None:
        self.kind = kind
        self.negated = negated
        if kind == omni.ABYSSAL_KIND:
            # The abyssal chip has no value stage of its own: picking the
            # kind mints the bare chip at once, and the card that opens on
            # it is where the module type gets chosen -- a second type list
            # here, in a one-line draft field, was the same choice twice.
            self._commit("")
            return
        prefix = _PREFIX_FOR_KIND.get(kind, kind)
        self.prefix_label.setText(("-" if negated else "") + prefix + ":")
        self.prefix_label.setVisible(True)
        self._restyle()
        self.edit.clear()
        self.edit.setPlaceholderText(_VALUE_PLACEHOLDER.get(kind, "value…"))
        self._on_edited("")  # surface the top values before anything is typed

    def _back_to_kind_stage(self) -> None:
        self.kind = None
        self.negated = False
        self.prefix_label.setVisible(False)
        self._restyle()
        self.edit.clear()
        self.edit.setPlaceholderText("filter type…")
        self.begin()

    # ----------------------------------------------------------- value stage
    def _on_edited(self, text: str) -> None:
        if self.kind is None:
            self._kind_completer.setCompletionPrefix(text.lstrip("-"))
            self._kind_completer.complete()
        elif self.kind == "is":
            flags = [f for f in omni.IS_FLAGS if f.startswith(text.strip().lower())]
            self.set_value_options([{"label": f, "stacks": None} for f in flags])
        elif (
            self.kind in _COMPLETION_COLUMN
            or self.kind in _ATTRIBUTE_KINDS
            or self.kind == omni.ABYSSAL_KIND
        ):
            self.value_fragment_edited.emit(self.kind, text)
        # val: has nothing to offer -- the comparison is the user's to write.

    def set_value_options(self, rows) -> None:
        """Fill the value popup; rows carry label and an optional stack count."""
        if self.kind is None:
            return
        self._value_model.clear()
        for row in rows:
            item = QStandardItem(_option_text(row))
            item.setEditable(False)
            item.setData(row["label"], _VALUE_ROLE)
            self._value_model.appendRow(item)
        if self._value_model.rowCount():
            self._value_completer.complete()

    def _value_picked(self, index: QModelIndex) -> None:
        value = index.data(_VALUE_ROLE)
        if value is None:
            return
        if self.kind in _ATTRIBUTE_KINDS:
            # The name is only half a stat: value; drop it into the field
            # and leave the operator and number to the user. setText does
            # not fire textEdited, so no fresh lookup reopens the popup.
            self.edit.setText(str(value))
            self.edit.setCursorPosition(len(self.edit.text()))
            popup = self._value_completer.popup()
            if popup is not None and popup.isVisible():
                popup.hide()
            return
        self._commit(str(value))

    def _on_return(self) -> None:
        if self.kind is None:
            resolved = self._resolve_kind(self.edit.text())
            if resolved is not None:
                self._enter_value_stage(*resolved)
            return
        if self.kind in _ATTRIBUTE_KINDS:
            self._commit_stat(self.edit.text())
            return
        value = self.edit.text().strip().strip('"')
        if value or self.kind == omni.ABYSSAL_KIND:
            # An empty abyssal value is the whole point of the kind ("every
            # abyssal item"); for anything else an empty value is no chip.
            self._commit(value)

    def _commit_stat(self, text: str) -> None:
        """Commit a stat: or roll: value only if the grammar accepts it, by
        asking the grammar. A draft that let `CPU usage` through without an
        operator would mint a chip whose SQL half has nothing to compare, so
        the card stays open until the value is one omni.parse would have
        minted -- the parser, not a second regex here, decides what that
        means."""
        raw = text.strip()
        if not raw:
            return
        # The placeholder teaches `"CPU usage"<30`; the value the grammar
        # holds is the unquoted `CPU usage<30`, which parse() derives from
        # the quoted token exactly as it would from typed text.
        prefix = _PREFIX_FOR_KIND[self.kind]
        spec = omni.parse(f"{prefix}:{raw}" if '"' in raw else f"{prefix}:{_quote(raw)}")
        chips = [c for c in spec.chips if c.kind == self.kind]
        if chips:
            self._commit(chips[0].value)

    def _commit(self, value: str) -> None:
        for completer in (self._kind_completer, self._value_completer):
            popup = completer.popup()
            if popup is not None and popup.isVisible():
                popup.hide()
        self.committed.emit(omni.Chip(self.kind, value, self.negated))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.edit and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.cancelled.emit()
                return True
            if (
                event.key() == Qt.Key_Backspace
                and self.kind is not None
                and not self.edit.text()
            ):
                self._back_to_kind_stage()
                return True
        return super().eventFilter(obj, event)


class Omnibox(QWidget):
    """Chip row plus embedded line edit, presenting as a single search field.

    changed() fires on every chip add/remove/clear and, debounced 220 ms, on
    text edits; chip_added(chip) accompanies each genuinely new chip so the
    view can sync outside state; escape_pressed() fires only when there is
    nothing left for escape to undo (see the module docstring's ladder).
    """

    changed = Signal()
    chip_added = Signal(object)
    escape_pressed = Signal()
    # The abyssal chip's glyph was clicked, or the chip was just typed:
    # (chip, the chip widget to anchor the card under). The omnibox stays
    # database-free about the card's contents; the view that owns the
    # queries builds and places it.
    card_requested = Signal(object, QWidget)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # Filter values that actually exist, used only to resolve unquoted
        # multi-word values while typing. Empty until the view supplies it,
        # which is a working state: those values just need quoting until then.
        self._vocabulary: dict = {}
        # A plain QWidget subclass ignores stylesheet backgrounds unless told
        # otherwise, so without this attribute the border below draws nothing
        # and the omnibox stops reading as one field.
        self.setObjectName("omnibox")
        self.setAttribute(Qt.WA_StyledBackground, True)
        # palette(shadow), not the mid role: mid is banned repo-wide by
        # test_no_view_still_uses_the_failing_role. As a border this one is
        # decorative rather than text, but a single vetted role for every
        # subdued element beats a per-case exemption in the sweep.
        self.setStyleSheet(
            "#omnibox { border: 1px solid palette(shadow); border-radius: 6px;"
            " background: palette(base); }"
        )

        # Not a QHBoxLayout: its minimum width is the sum of its chips', and
        # chips have no upper bound. flow_layout.py documents the failure.
        self._row = FlowLayout(self)
        self._row.setContentsMargins(8, 4, 8, 4)
        self._row.setSpacing(6)

        icon = QLabel("⌕")
        icon.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self._row.addWidget(icon)

        # Mouse-side entrance to the draft-chip builder. Ctrl+F is the
        # keyboard one, bound once by the hosting AssetsView with
        # WidgetWithChildrenShortcut -- a second binding here made the two
        # match together whenever focus sat in the line edit, and Qt answers
        # an ambiguous match by firing neither. Chips insert at index
        # 1 + len(chips), so they stack between the glyph and this button and
        # the + stays at the row's working edge.
        self.add_btn = _PlusButton()
        self.add_btn.clicked.connect(self.open_draft)
        self._row.addWidget(self.add_btn)

        self.edit = QLineEdit()
        self.edit.setFrame(False)
        self.edit.setStyleSheet("background: transparent;")
        self.edit.setPlaceholderText(
            "Search, or filter with loc: owner: cat: is: val: abyssal roll: …"
        )
        self._row.addWidget(self.edit)

        self.hint = QLabel("/ to search")
        self.hint.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self._row.addWidget(self.hint)
        # The edit takes whatever is left of the line the chips end on, with
        # the hint riding after it; too little room and both drop to a fresh
        # line rather than leaving a slot too narrow to type in.
        self._row.set_fill(self.edit, trailer=self.hint)

        self._chips: list[tuple[omni.Chip, _ChipWidget]] = []

        # The shared debounce every search box in the app uses: text settles
        # for 220 ms before the (expensive, table-reloading) changed() goes
        # out. Chip operations bypass it -- a click is already a settled
        # intent.
        self._debounce = Debounce(self, self.changed)

        self._completion_model = QStandardItemModel(self)
        self._completer = QCompleter(self)
        self._completer.setModel(self._completion_model)
        self._completer.setWidget(self.edit)
        # The SQL already narrowed the candidates, so the completer must not
        # filter a second time against the full field text (which starts with
        # the token prefix and would match nothing).
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self._completer.setMaxVisibleItems(10)
        self._completer.activated[QModelIndex].connect(self._apply_completion)
        self._complete_query = AsyncQuery(self)

        self.edit.textEdited.connect(self._on_text_edited)
        self.edit.textChanged.connect(lambda _text: self._sync_hint())
        self.edit.returnPressed.connect(self._commit_text)
        self.edit.installEventFilter(self)
        self._sync_hint()

        self._draft: _DraftChip | None = None

    # ------------------------------------------------------------------- API
    def add_chip(self, kind: str, value: str, negated: bool = False) -> None:
        """Add one chip; an identical chip already present makes this a no-op
        (rail rows and context menus re-send the same filter freely)."""
        chip = omni.Chip(kind, value, negated)
        if self._insert_chip(chip):
            self.chip_added.emit(chip)
            self._emit_changed_now()

    def remove_chip(self, chip: omni.Chip) -> None:
        for position, (existing, widget) in enumerate(self._chips):
            if existing == chip:
                del self._chips[position]
                self._row.removeWidget(widget)
                widget.deleteLater()
                self._emit_changed_now()
                return

    def spec(self) -> omni.FilterSpec:
        """The current filter: chips plus whatever bare text is in the field."""
        return omni.FilterSpec(
            text=self.edit.text().strip(),
            chips=[chip for chip, _widget in self._chips],
        )

    def set_spec(self, spec: omni.FilterSpec) -> None:
        """Rebuild from a saved spec with a single changed() at the end --
        per-chip signals here would fire one table reload per chip while
        restoring a saved view."""
        self._remove_all_chip_widgets()
        self._chips = []
        for chip in spec.chips:
            self._insert_chip(chip)
        self.edit.setText(spec.text)
        self._hide_completions()
        self._emit_changed_now()

    def clear(self) -> None:
        self._remove_all_chip_widgets()
        self._chips = []
        self.edit.clear()
        self._hide_completions()
        self._emit_changed_now()

    def focus_search(self) -> None:
        self.edit.setFocus(Qt.ShortcutFocusReason)

    def open_draft(self) -> None:
        """Open (or refocus) the draft-chip builder card."""
        if self._draft is not None:
            self._draft.edit.setFocus(Qt.ShortcutFocusReason)
            return
        draft = _DraftChip()
        self._draft = draft
        # Same slot a finished chip would take: after the existing chips,
        # before the + button and the line edit.
        self._row.insertWidget(1 + len(self._chips), draft)
        draft.committed.connect(self._on_draft_committed)
        draft.cancelled.connect(self._close_draft)
        draft.value_fragment_edited.connect(self._complete_for_draft)
        # Force the pending layout pass NOW: begin() opens the kind popup,
        # and QCompleter anchors it to the edit's geometry at that moment --
        # anchored to the not-yet-laid-out card, the list appeared floating
        # halfway down the table instead of under the field.
        self._row.activate()
        draft.begin()

    def _on_draft_committed(self, chip: omni.Chip) -> None:
        self._close_draft()
        if self._insert_chip(chip):
            self.chip_added.emit(chip)
            self._emit_changed_now()
            self._request_card_later([chip])

    def _close_draft(self) -> None:
        if self._draft is None:
            return
        draft = self._draft
        self._draft = None
        self._row.removeWidget(draft)
        draft.deleteLater()
        self.edit.setFocus(Qt.ShortcutFocusReason)

    def _complete_for_draft(self, kind: str, fragment: str) -> None:
        """Value options for the draft's stage 2 -- the same COUNT projection
        the token completer uses, delivered to the card instead of the main
        popup. One AsyncQuery serves both: only one of the two completers is
        ever active, and the generation guard already handles the overlap."""
        fetch = _completion_fetch(kind, fragment.strip().strip('"'))
        if fetch is None:
            return
        self._complete_query.run(
            fetch,
            lambda rows: self._draft is not None and self._draft.set_value_options(rows),
            lambda _message: None,
        )

    # ----------------------------------------------------------------- chips
    def _insert_chip(self, chip: omni.Chip) -> bool:
        if any(existing == chip for existing, _widget in self._chips):
            return False
        widget = _ChipWidget(chip)
        widget.close_btn.clicked.connect(lambda _checked=False, c=chip: self.remove_chip(c))
        if widget.card_btn is not None:
            widget.card_btn.clicked.connect(
                lambda _checked=False, c=chip, w=widget: self.card_requested.emit(c, w)
            )
        # Chips sit between the search glyph (index 0) and the line edit, in
        # the order they were added, so the row reads left to right as the
        # filter accumulated.
        self._row.insertWidget(1 + len(self._chips), widget)
        self._chips.append((chip, widget))
        return True

    def _remove_all_chip_widgets(self) -> None:
        for _chip, widget in self._chips:
            self._row.removeWidget(widget)
            widget.deleteLater()

    def _emit_changed_now(self) -> None:
        # A pending debounce would re-announce state this emission already
        # covers; stop it so listeners see one changed() per user action.
        self._debounce.stop()
        self.changed.emit()

    # ------------------------------------------------------------ text entry
    def _on_text_edited(self, text: str) -> None:
        # A trailing space commits any fully typed tokens -- but not while a
        # quote is open: 'loc:"Jita ' is a value in progress, not a token.
        if text.endswith(" ") and text.count('"') % 2 == 0 and self._migrate_tokens():
            self._hide_completions()
            self._emit_changed_now()
            return
        self._debounce.trigger()
        self._maybe_complete(text)

    def _commit_text(self) -> None:
        # Enter means "apply now": migrate token text into chips and emit
        # immediately instead of waiting out the debounce. The open-quote
        # guard matches the space-commit path above -- committing
        # 'loc:"Jita IV ' mid-quote would mint a chip whose value carries an
        # invisible trailing space and exact-matches nothing.
        before = [chip for chip, _widget in self._chips]
        parsed: list[omni.Chip] = []
        if self.edit.text().count('"') % 2 == 0:
            parsed = self._migrate_tokens()
        self._hide_completions()
        self._emit_changed_now()
        self._request_card_later([chip for chip in parsed if chip not in before])

    def set_vocabulary(self, vocabulary: dict) -> None:
        """The values that actually exist, per chip kind.

        Only used to resolve unquoted multi-word values while typing, so it is
        allowed to be stale or absent: without it those values simply need
        quoting, which is what they needed before.
        """
        self._vocabulary = vocabulary or {}

    def _migrate_tokens(self) -> list[omni.Chip]:
        """Turn the field's finished tokens into chips and return the parsed
        ones, minted or already present -- empty when the text held none."""
        spec = omni.parse(self.edit.text(), self._vocabulary)
        if not spec.chips:
            return []
        for chip in spec.chips:
            if self._insert_chip(chip):
                self.chip_added.emit(chip)
        self.edit.setText(spec.text)
        return spec.chips

    def _request_card_later(self, minted: list[omni.Chip]) -> None:
        """Open the card under an abyssal chip the user has just typed or
        built, without a glyph click: typing the word and pressing Enter is
        the natural way in, and the card is the point of the chip.

        Only the freshly minted, positive chip qualifies. A chip that arrives
        by set_spec (a saved view, the card's own Done) or add_chip (the
        rail, a context menu) is being restored or placed, not asked for,
        and Done re-opening the card it just closed would loop; a negated
        chip is one the card cannot express. The trailing-space commit does
        not qualify either: a Qt.Popup steals the keyboard, and the user who
        typed ``abyssal `` is on the way to ``roll:web>=70``.

        Deferred a turn because a Qt.Popup shown in the same event turn as
        the chip widget is inserted into the layout is closed by Qt before
        it is seen (pinned in tests/test_omnibox.py); the chip is looked
        up again when the timer fires, since a cross or a set_spec may have
        removed it in between.
        """
        chip = next((c for c in minted if c.kind == omni.ABYSSAL_KIND and not c.negated), None)
        if chip is None:
            return
        QTimer.singleShot(0, self, lambda: self._request_card(chip))

    def _request_card(self, chip: omni.Chip) -> None:
        widget = next((w for c, w in self._chips if c == chip), None)
        if widget is not None:
            self.card_requested.emit(chip, widget)

    def _escape(self) -> None:
        if self.edit.text():
            self.edit.clear()
            self._hide_completions()
            self._emit_changed_now()
        elif self._chips:
            self.remove_chip(self._chips[-1][0])
        else:
            self.escape_pressed.emit()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.edit:
            if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self._escape()
                return True
            if event.type() in (QEvent.FocusIn, QEvent.FocusOut):
                self._sync_hint()
        return super().eventFilter(obj, event)

    def _sync_hint(self) -> None:
        self.hint.setVisible(not self.edit.hasFocus() and not self.edit.text())

    # ------------------------------------------------------------ completion
    def _maybe_complete(self, text: str) -> None:
        _head, token = _split_trailing_token(text)
        match = _TOKEN_RE.match(token)
        kind = _KIND_FOR_PREFIX.get(match.group(2).lower()) if match else None
        if kind is None:
            self._hide_completions()
            return
        negated = match.group(1) == "-"
        partial = match.group(3).lstrip('"')
        fetch = _completion_fetch(kind, partial)
        if fetch is None:
            self._hide_completions()
            return

        # One query per keystroke; AsyncQuery's generation guard drops any
        # result a newer keystroke has already superseded. A failed lookup
        # only costs the popup -- typing and filtering must keep working.
        self._complete_query.run(
            fetch,
            lambda rows: self._show_completions(token, kind, negated, rows),
            lambda _message: self._hide_completions(),
        )

    def _show_completions(self, token: str, kind: str, negated: bool, rows: list) -> None:
        _head, current = _split_trailing_token(self.edit.text())
        if current != token:
            # The generation guard drops superseded queries, but a commit or
            # programmatic setText can change the field between this query
            # being the latest one and its result landing.
            return
        self._completion_model.clear()
        for row in rows:
            item = QStandardItem(_option_text(row))
            item.setEditable(False)
            item.setData(row["label"], _VALUE_ROLE)
            item.setData(kind, _KIND_ROLE)
            item.setData(negated, _NEGATED_ROLE)
            self._completion_model.appendRow(item)
        if not rows:
            self._hide_completions()
            return
        self._completer.complete()

    def _apply_completion(self, index: QModelIndex) -> None:
        value = index.data(_VALUE_ROLE)
        if value is None:
            return
        kind, negated = index.data(_KIND_ROLE), bool(index.data(_NEGATED_ROLE))
        head, _token = _split_trailing_token(self.edit.text())
        if kind in _ATTRIBUTE_KINDS:
            # Half a value: write `stat:"CPU usage"` back into the field for
            # the user to finish with an operator and number. The quoted
            # name unquotes inside parse() the same way a whole quoted value
            # would, so `stat:"CPU usage"<30` and `stat:"CPU usage<30"` are
            # the same chip.
            prefix = _PREFIX_FOR_KIND[kind]
            self.edit.setText(f"{head}{'-' if negated else ''}{prefix}:{_quote(str(value))}")
            self.edit.setCursorPosition(len(self.edit.text()))
            self._hide_completions()
            return
        chip = omni.Chip(kind, value, negated)
        self.edit.setText(head.rstrip())
        self._hide_completions()
        if self._insert_chip(chip):
            self.chip_added.emit(chip)
        # The token text left the field even if the chip was a duplicate, so
        # listeners must hear about it either way.
        self._emit_changed_now()

    def _hide_completions(self) -> None:
        popup = self._completer.popup()
        if popup is not None and popup.isVisible():
            popup.hide()
