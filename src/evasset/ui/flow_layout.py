"""A wrapping row layout for the omnibox.

QHBoxLayout's minimum width is the sum of its children's, so a field holding
six station-name chips demanded a row wider than a full-screen window and
the whole tab -- rail, export button, table columns -- was shoved off the
right edge. Chips are filter state, and filter state has no natural upper
bound, so the row has to wrap rather than grow.

This is the classic flow layout with one addition the omnibox needs: one
widget (the line edit) *fills* whatever is left of the line it lands on, and
one more (the keyboard hint) is pinned immediately after it on that same
line. The fill widget carries a minimum width of its own; when a line has
less than that left, the edit and its hint drop to a fresh line together
rather than squeezing into a slot too narrow to type in. Everything else
flows left to right in insertion order and wraps at the right margin, which
keeps the "chips read in the order they were added" property the omnibox
documents.

heightForWidth is the whole point: the enclosing box layouts ask it once per
resize and the omnibox grows downward by whole lines, pushing the state row
and table down, instead of sideways off the screen.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget, QWidgetItem


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, *, fill_min_width: int = 160):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._fill: QWidget | None = None
        self._trailer: QWidget | None = None
        self._fill_min_width = fill_min_width

    # ----------------------------------------------------------- population
    def set_fill(self, widget: QWidget, trailer: QWidget | None = None) -> None:
        """Nominate the widget that takes the remainder of its line, and the
        one (if any) that rides along right after it. Both must also be added
        to the layout in the usual way; this only changes how they are placed."""
        self._fill = widget
        self._trailer = trailer
        self.invalidate()

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)
        self.invalidate()

    def insertWidget(self, index: int, widget: QWidget) -> None:  # noqa: N802
        """Positional insert, which QLayout itself lacks -- the omnibox keeps
        chips between the search glyph and the + button by index."""
        self.addChildWidget(widget)
        self._items.insert(index, QWidgetItem(widget))
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self.invalidate()
            return item
        return None

    # --------------------------------------------------------------- sizing
    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802
        # Wants width, never height: the height is a function of the width.
        return Qt.Horizontal

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        # Deliberately NOT the unwrapped width. A hint of "everything on one
        # line" is exactly what the box layouts above would honour, and the
        # window would size itself around a chip row again.
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        margins = self.contentsMargins()
        width = self._fill_line_width()
        height = 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            height = max(height, hint.height())
            if item.widget() not in (self._fill, self._trailer):
                width = max(width, item.minimumSize().width())
        return QSize(
            width + margins.left() + margins.right(),
            height + margins.top() + margins.bottom(),
        )

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    # --------------------------------------------------------------- layout
    def _trailer_width(self) -> int:
        for item in self._items:
            if item.widget() is self._trailer and not item.isEmpty():
                return item.sizeHint().width()
        return 0

    def _reserved_width(self) -> int:
        """Width the trailer claims after the fill widget: its own plus the
        gap before it, or nothing when it is absent or hidden."""
        trailer = self._trailer_width()
        return self.spacing() + trailer if trailer else 0

    def _fill_line_width(self) -> int:
        """Width the fill widget and its trailer need to share a line."""
        return self._fill_min_width + self._reserved_width()

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        """Place every visible item, or only measure. Returns the height the
        arrangement needs, margins included."""
        margins = self.contentsMargins()
        left = rect.x() + margins.left()
        right = rect.x() + rect.width() - margins.right()
        spacing = self.spacing()
        reserved = self._reserved_width()
        fill_line = self._fill_min_width + reserved
        x, y = left, rect.y() + margins.top()
        line: list[tuple[QLayoutItem, QRect]] = []
        line_height = 0
        placed_any = False

        def flush() -> None:
            nonlocal x, y, line, line_height, placed_any
            if apply:
                for item, geometry in line:
                    # Centre each item vertically on its line; chips, the
                    # plus tile and the edit are all slightly different
                    # heights and a top-aligned row looks ragged.
                    offset = (line_height - geometry.height()) // 2
                    item.setGeometry(geometry.translated(QPoint(0, offset)))
            y += line_height + spacing
            x = left
            line = []
            line_height = 0
            placed_any = True

        for item in self._items:
            if item.isEmpty():
                continue
            widget = item.widget()
            hint = item.sizeHint()
            if widget is self._fill:
                if line and x + fill_line > right:
                    flush()
                width = max(self._fill_min_width, right - x - reserved)
            elif widget is self._trailer:
                # Placed by the fill's reservation; never wraps on its own.
                width = hint.width()
            else:
                width = hint.width()
                if line and x + width > right:
                    flush()
            line.append((item, QRect(x, y, width, hint.height())))
            line_height = max(line_height, hint.height())
            x += width + spacing
        if line:
            flush()
        # flush() spaces lines apart; the last line must not carry that gap
        # into the bottom margin.
        bottom = y - spacing if placed_any else y
        return bottom + margins.bottom() - rect.y()
