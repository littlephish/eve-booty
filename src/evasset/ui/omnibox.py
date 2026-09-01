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
nothing to offer for either.
"""

from __future__ import annotations

import re
import sqlite3

from PySide6.QtCore import QEvent, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from .. import omni, queries
from . import palette
from .async_query import AsyncQuery
from .debounce import Debounce

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
}
_PREFIX_FOR_KIND = {kind: prefix for prefix, kind in _KIND_FOR_PREFIX.items()}

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
_ALL_KINDS = (*omni.LEVEL_KINDS, "item", "is", "val")

# Placeholder per chosen kind: the draft's value stage should say what kind
# of thing it wants, because "value…" teaches nothing for the two kinds
# whose vocabulary is fixed.
_VALUE_PLACEHOLDER = {
    "is": " / ".join(omni.IS_FLAGS),
    "val": ">10m   <1b   >=500k …",
}

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


class _ChipWidget(QFrame):
    """One removable filter token: muted ``kind:`` prefix, the value, and a
    cross. The prefix is muted because the value is the payload -- the kind is
    scaffolding the eye should be able to skip once the colour has already
    said which axis this chip filters (each kind wears its own wash) and, in
    red, whether it excludes rather than includes."""

    def __init__(self, chip: omni.Chip, parent: QWidget | None = None):
        super().__init__(parent)
        wash = palette.chip_tint(chip.kind, chip.negated)
        self.setObjectName("chip")
        self.setStyleSheet(f"#chip {{ background: {wash}; border-radius: 9px; }}")

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 2, 3, 2)
        row.setSpacing(2)

        prefix = ("-" if chip.negated else "") + _PREFIX_FOR_KIND.get(chip.kind, chip.kind) + ":"
        self.prefix_label = QLabel(prefix)
        self.prefix_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        row.addWidget(self.prefix_label)

        self.value_label = QLabel(chip.value)
        row.addWidget(self.value_label)

        self.close_btn = QToolButton()
        self.close_btn.setText("×")
        self.close_btn.setAutoRaise(True)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("Remove this filter")
        self.close_btn.setStyleSheet("QToolButton { border: none; padding: 0 2px; }")
        row.addWidget(self.close_btn)


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
        elif self.kind in _COMPLETION_COLUMN or self.kind == "item":
            self.value_fragment_edited.emit(self.kind, text)
        # val: has nothing to offer -- the comparison is the user's to write.

    def set_value_options(self, rows) -> None:
        """Fill the value popup; rows carry label and an optional stack count."""
        if self.kind is None:
            return
        self._value_model.clear()
        for row in rows:
            count = row["stacks"]
            text = row["label"] if count is None else f"{row['label']} · {count:,}"
            item = QStandardItem(text)
            item.setEditable(False)
            item.setData(row["label"], _VALUE_ROLE)
            self._value_model.appendRow(item)
        if self._value_model.rowCount():
            self._value_completer.complete()

    def _value_picked(self, index: QModelIndex) -> None:
        value = index.data(_VALUE_ROLE)
        if value is not None:
            self._commit(str(value))

    def _on_return(self) -> None:
        if self.kind is None:
            resolved = self._resolve_kind(self.edit.text())
            if resolved is not None:
                self._enter_value_stage(*resolved)
            return
        value = self.edit.text().strip().strip('"')
        if value:
            self._commit(value)

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

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
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

        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(8, 4, 8, 4)
        self._row.setSpacing(6)

        icon = QLabel("⌕")
        icon.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self._row.addWidget(icon)

        # Mouse-side entrance to the draft-chip builder; Ctrl+F is the
        # keyboard one. Chips insert at index 1 + len(chips), so they stack
        # up between the glyph and this button and the + stays at the row's
        # working edge.
        self.add_btn = QToolButton()
        self.add_btn.setText("+")
        self.add_btn.setAutoRaise(True)
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setToolTip("Build a filter (Ctrl+F)")
        self.add_btn.clicked.connect(self.open_draft)
        self._row.addWidget(self.add_btn)

        self.edit = QLineEdit()
        self.edit.setFrame(False)
        self.edit.setStyleSheet("background: transparent;")
        self.edit.setPlaceholderText("Search, or filter with loc: owner: cat: is: val: …")
        self._row.addWidget(self.edit, 1)

        self.hint = QLabel("/ to search")
        self.hint.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self._row.addWidget(self.hint)

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
        # WidgetWithChildrenShortcut so the builder opens whether the focus
        # sits in the line edit, on a chip's cross, or in the draft itself.
        QShortcut(
            QKeySequence("Ctrl+F"),
            self,
            context=Qt.WidgetWithChildrenShortcut,
            activated=self.open_draft,
        )

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
        self.add_chip(chip.kind, chip.value, chip.negated)

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
        column = _COMPLETION_COLUMN.get(kind)
        if column is None:
            return
        sql = (
            f"SELECT {column} AS label, COUNT(*) AS stacks"
            f" FROM ({queries.ASSET_ROWS})"
            " WHERE label IS NOT NULL AND label LIKE ? ESCAPE '\\'"
            " GROUP BY label ORDER BY stacks DESC, label COLLATE NOCASE LIMIT 12"
        )
        pattern = _like_pattern(fragment.strip().strip('"'))

        def fetch(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return list(conn.execute(sql, (pattern,)))

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
        if self.edit.text().count('"') % 2 == 0:
            self._migrate_tokens()
        self._hide_completions()
        self._emit_changed_now()

    def _migrate_tokens(self) -> bool:
        spec = omni.parse(self.edit.text())
        if not spec.chips:
            return False
        for chip in spec.chips:
            if self._insert_chip(chip):
                self.chip_added.emit(chip)
        self.edit.setText(spec.text)
        return True

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
        # The one piece of SQL in this file is a projection of
        # queries.ASSET_ROWS: building on the exact same SELECT guarantees
        # the labels offered here are precisely the strings the resulting
        # chip will filter on. Its shape (LIKE, count ordering, LIMIT) is a
        # property of this popup, not a reusable read, so it lives beside the
        # completer rather than in queries.py.
        sql = (
            f"SELECT {_COMPLETION_COLUMN[kind]} AS label, COUNT(*) AS stacks"
            f" FROM ({queries.ASSET_ROWS})"
            " WHERE label IS NOT NULL AND label LIKE ? ESCAPE '\\'"
            " GROUP BY label ORDER BY stacks DESC, label COLLATE NOCASE LIMIT 12"
        )
        pattern = _like_pattern(partial)

        def fetch(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return list(conn.execute(sql, (pattern,)))

        # One query per keystroke; AsyncQuery's generation guard drops any
        # result a newer keystroke has already superseded. A failed lookup
        # only costs the popup -- typing and filtering must keep working.
        self._complete_query.run(
            fetch,
            lambda rows: self._show_completions(token, kind, negated, rows),
            lambda _message: self._hide_completions(),
        )

    def _show_completions(
        self, token: str, kind: str, negated: bool, rows: list[sqlite3.Row]
    ) -> None:
        _head, current = _split_trailing_token(self.edit.text())
        if current != token:
            # The generation guard drops superseded queries, but a commit or
            # programmatic setText can change the field between this query
            # being the latest one and its result landing.
            return
        self._completion_model.clear()
        for row in rows:
            item = QStandardItem(f"{row['label']} · {row['stacks']:,}")
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
        chip = omni.Chip(index.data(_KIND_ROLE), value, bool(index.data(_NEGATED_ROLE)))
        head, _token = _split_trailing_token(self.edit.text())
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
