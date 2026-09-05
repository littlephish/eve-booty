"""The abyssal complex-search card: module type, then a range track per rolled stat.

A mutated module has three to eight rolled stats and nothing about them is
searchable in game, so the question the estate actually poses -- "which of my
webifiers rolled strength AND range well" -- is a multi-attribute range query
over one module type. The grammar can say that (``abyssal:"Abyssal Stasis
Webifier" stat:web<=-60 stat:range>=12``), but nobody should have to type
it: this card builds exactly those chips with a type picker and a two-handle
track per stat, and hands them back to the omnibox on Done.

It is a popover (Qt.Popup) anchored under the abyssal chip rather than a
dialog, because it edits the filter the chip already shows -- a modal window
over the table would hide the very rows the filter is narrowing. Popup
semantics decide the commit model too: Qt closes a popup on any outside
click, and that close has to mean something. It means Cancel. Done is the
only path that applies, so an exploratory drag that gets abandoned by
clicking back on the table leaves the filter exactly as it was.

A row bounds a stat in its display units -- the tf, % or m the table, the
inspector and the chip all show -- and in nothing else. A quality-percent
axis was rejected: every other surface speaks in the stat's own units, so a
percent is a figure the user has to translate before trusting it. The
``roll:`` grammar still exists for typing; the card neither builds nor
edits a ``roll:`` chip, and one typed beside the card's own rides through
Done untouched.

The track runs WORST to BEST, left to right, the way the inspector's roll
meters do, whichever way the number itself runs: a CPU track has its big
number on the left, a velocity bonus its small one. Dragging the right
handle inward therefore always means "only the better rolls", on every
row, without reading a unit -- a numerically ascending slider would make
the same gesture mean opposite things on a CPU row and a damage row. The
two fields beside the track follow the same orientation (left is the
worst-side bound), and the labels under it spell out which value each end
is, with the type's un-mutated base ticked between them.

Done canonicalises what it re-emits: a chip typed as ``stat:cpu>40`` comes
back as ``stat:CPU usage>=40``, because a row knows its attribute by id and
names it the way the picker does, and a track has no handle position for
"just above 40" -- a strict operator seeds onto the inclusive handle at the
same number. The filter means the same rows before and after (the grammar
resolves both spellings to one attribute, and the one item sitting exactly
on the bound is the only difference between > and >=); only the spelling
settles.

The card is database-free. The view that owns the queries feeds it the type
counts, the attribute list (with polarity and base) and bounds for the
selected type, the not-fetched count and the live match count, and hears
back which type is selected (so it can fetch that type's attributes), that
the filter changed (so it can count), which items to fetch, and the final
chips.

The type picker is a single-select dropdown of the module types the estate
holds, rather than a checkable list, and it can hold no selection at all:
an empty edit under its placeholder is the bare ``abyssal`` chip, every
dynamic type. There is no "All abyssal modules" entry: the chip already
means every abyssal item until a type is picked, so such an entry would
restate the state the card opened in and sit among the types as if it were
one. Clearing the edit -- and Enter, or leaving it -- is the way back from a
type to every item. Stat rows are enabled only with one type picked: every
type rolls a different attribute set, so a track over two types would have
no honest bounds to run between, and a picker that could tick two types
would spend most of its height offering a state in which the rest of the
card went dark. The grammar still says several types (``abyssal:"Abyssal
Stasis Webifier, Abyssal Warp Disruptor"``, types OR), and a saved view can
carry one; seeded with such a chip the dropdown shows the first type and
Done writes that one alone -- the multi-type chip stays a typed affair,
which is the only place it can be built anyway.

The dropdown can be typed into. An estate that runs abyssals holds dozens of
dynamic types, and the list orders them by count rather than by name, so
finding one by scrolling means reading the whole list; the edit takes a few
letters and a completer narrows the list to the entries containing them --
matched against the entry's label and the full SDE name both, so "abyssal
stasis" pasted from a fit still lands on "Stasis Webifier · 12". Enter takes
the exact entry when the text is one, else the highlighted or first match,
reverts the text when nothing matches, and deselects the type when the text
is empty: the edit is a search box over the list, never a free-text field,
because the only values the chip can carry are the types the estate holds
or none of them. The arrow and the full list are still there for browsing.
"""

from __future__ import annotations

import math
import re

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import abyssal, omni
from . import palette
from .meters import paint_segments
from .strip import caption_font

CARD_WIDTH = 440
FIELD_WIDTH, FIELD_HEIGHT = 76, 24

# The chip kinds this card owns: what it seeds from, and what the view
# replaces with its output on Done. roll: is deliberately absent -- the card
# cannot express a quality bound, so a typed roll: chip has to survive Done
# rather than be swallowed by the replacement.
CARD_KINDS = (omni.ABYSSAL_KIND, omni.STAT_KIND)

# What the type completer matches against: the entry's label followed by the
# full SDE name, so "web", "stasis webifier · 12" and "abyssal stasis" all
# find the webifier. The label alone would miss the full name (the label
# drops the "Abyssal" the game puts on every dynamic type) and the name alone
# would miss the label's count.
_SEARCH_ROLE = Qt.UserRole + 1
TYPE_PLACEHOLDER = "Type to search, or pick…"
COMPLETIONS_VISIBLE = 12

LEFT, RIGHT = "left", "right"


def _number(value: float, decimals: int) -> str:
    """A bound as the grammar wants it: fixed decimals, trailing zeros and a
    bare point dropped, and never "-0" -- the number in the chip should read
    as the number in the field, not as a float's idea of it."""
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text


# The track stores handle fractions, so a bound typed as 26 comes back from
# values() as 25.999999999999996 or 26.000000000000004 -- noise around the
# fourteenth digit that outward rounding would otherwise turn into a visibly
# widened 25.9. Real display values never sit that close to a decimal edge:
# ESI serves float32 (25.799999713897705 for 25.8), three parts in ten
# million off, so a nudge of one part in a billion of the last place tells
# the two apart with room to spare.
_ROUNDING_SLACK = 1e-9


def _outward(value: float, decimals: int, up: bool) -> float:
    """Round a bound AWAY from the range, to the row's decimals.

    The field shows 25.8 for a CPU of 25.799999713897705, and stat:
    compares against the exact display value, so a chip written from the
    field's own number -- ``CPU usage=25.8..26.3`` -- would drop the very
    item whose bounds the track was scoped to. The low bound floors and the
    high bound ceils, so whatever the track admits the chip admits too:
    toward minus infinity for the low end even when negative (-62.95 becomes
    -63), toward plus infinity for the high end (-51.05 becomes -51).
    """
    scale = 10 ** decimals
    scaled = value * scale
    if up:
        return math.ceil(scaled - _ROUNDING_SLACK) / scale
    return math.floor(scaled + _ROUNDING_SLACK) / scale


def _decimals(lo: float, hi: float) -> int:
    """Field precision for a bound pair, by magnitude: two places under
    ten (a 1.11x multiplier), one under a hundred (a 27.4 tf CPU), none
    above (a 3,318 HP figure) -- the same instinct as abyssal.format_value."""
    magnitude = max(abs(lo), abs(hi))
    if magnitude < 10:
        return 2
    if magnitude < 100:
        return 1
    return 0


def _unit_suffix(attr: dict) -> str:
    unit = attr.get("unit")
    if not unit:
        return ""
    return unit if unit in ("%", "x") else f" {unit}"


def _fmt(value: float, decimals: int, attr: dict) -> str:
    """A value the way the row's fields and labels show it: the row's
    decimals, the unit symbol, and a forced sign for the modifier units
    (abyssal.SIGNED_UNITS) -- a webifier's "-60%" and a damage mod's "+10%"
    read as the game shows them. format_value is not used because its own
    precision rule (two decimals under ten, none above) would show a 27.4
    tf field as "27 tf" and then commit 27 on the next Enter."""
    text = _number(value, decimals)
    if attr.get("unit_id") in abyssal.SIGNED_UNITS and not text.startswith("-") and text != "0":
        text = f"+{text}"
    return f"{text}{_unit_suffix(attr)}"


_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)")


def _parse_number(text: str) -> float | None:
    """The number in a field's text, or None. Accepts what the field itself
    shows ("+11%", "26 tf", "1,200 HP") as well as a bare number, so a
    user who edits one digit of the shown text and presses Enter commits
    the obvious value; anything without a number in it is garbage."""
    match = _NUMBER.search(text.replace(",", "").strip())
    if match is None:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def attribute_chip_name(attr: dict) -> str:
    """The name a stat: chip should carry for one attribute: the display
    name when the grammar resolves it to this attribute alone, CCP's
    internal name otherwise.

    queries.abyssal_type_attributes already disambiguates a shared display
    name by appending the unit ("Signature Radius Modifier (%)"), which is
    how a shared one is recognised here -- that suffixed label is not a name
    the grammar matches, and the display name without it matches both
    namesakes. A display name that is also a curated alias for a different
    attribute (omni resolves aliases first) takes the internal name for the
    same reason.
    """
    name, label, unit = str(attr["name"]), str(attr["label"]), attr.get("unit")
    if label.endswith(f" ({name})") or (unit and label.endswith(f" ({unit})")):
        return name
    alias_target = abyssal.STAT_ALIASES.get(label.lower())
    if alias_target is not None and alias_target != name:
        return name
    if any(ch in label for ch in "<>=") or not label.strip():
        return name
    return label


def _matches(attr: dict, typed: str) -> bool:
    """Does a chip's attribute name mean this attribute -- by internal name,
    display label (with or without the unit suffix), or alias."""
    needle = typed.strip().lower()
    if not needle:
        return False
    if needle == str(attr["name"]).lower():
        return True
    label = str(attr["label"]).lower()
    if needle == label:
        return True
    # The disambiguated label shapes queries.abyssal_type_attributes emits:
    # a shared display name suffixed with its unit, or its internal name.
    suffixes = [str(attr["name"]).lower()]
    if attr.get("unit"):
        suffixes.append(str(attr["unit"]).lower())
    if any(label == f"{needle} ({suffix})" for suffix in suffixes):
        return True
    return abyssal.STAT_ALIASES.get(needle) == attr["name"]


class _RangeTrack(QWidget):
    """Two handles on a segmented track that runs worst to best.

    Qt ships no dual-handle slider (QSlider is single-valued and superqt is
    not a dependency). This one keeps its state as 0..1 fractions of the
    track rather than as values in the caller's units, the same way the
    rail's _ValueBar and the inspector's _RollMeter store fractions: the
    geometry is then testable offscreen where nothing is ever painted, and
    "at the bound" is a fraction test the card can trust, where comparing a
    computed value against the bound in floating point would miss by a
    rounding error exactly when it mattered.

    The axis is oriented by the caller: worst is the value at fraction 0
    and best at fraction 1, and the two may run numerically downward (a CPU
    track goes 31 -> 24). values() reports the value at each handle in that
    left-to-right order; the row sorts them when it needs a numeric pair.

    The track is the inspector meter's segment rhythm (meters.paint_segments),
    the selection between the handles the same segments in the meter's
    verdict colours, split at the base tick: red on the worse side of the
    source module's value, green on the better side, so a range reads the
    way a roll does on the inspector. With no base the rail bar's accent
    stands in, since there is no side to be on. The base tick and the
    handles are the text colour; the handles wear a 1 px
    ring in the window colour so they stay distinct from the tick and from
    each other when the two meet. The handles overhang the track and the
    track is inset by half a handle each side so a handle at either end
    stays inside the widget instead of being clipped to a sliver.

    moved fires on user interaction only -- set_values is the caller
    telling the track where things are, and echoing that back would have
    every field/track pair in the card ping-pong.
    """

    moved = Signal()

    TRACK_HEIGHT = 6
    HANDLE_WIDTH, HANDLE_HEIGHT, HANDLE_RADIUS = 8, 14, 2
    TICK_WIDTH, TICK_HEIGHT = 2, 10
    HEIGHT = HANDLE_HEIGHT + 2      # the ring around the handles
    INSET = HANDLE_WIDTH // 2 + 1
    STEP = 0.01     # arrow keys: one percent of the track
    PAGE = 0.10     # with Shift

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.worst = 0.0
        self.best = 1.0
        self.base_fraction: float | None = None
        self.low_fraction = 0.0
        self.high_fraction = 1.0
        # The handle the keyboard moves and the last one the mouse touched.
        self.active = RIGHT
        self._dragging: str | None = None
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.SizeHorCursor)

    # ------------------------------------------------------------------- API
    def set_axis(self, worst: float, best: float, base: float | None = None) -> None:
        """The values the two ends of the track stand for, and the base to
        tick between them (None, or a base outside the two, draws no tick:
        clamped to an end it would claim the base is the worst or best roll
        owned, when it is really worse or better than all of them). Handles
        keep their fractions."""
        self.worst, self.best = float(worst), float(best)
        self.base_fraction = None
        if base is not None and self.worst != self.best:
            fraction = (float(base) - self.worst) / (self.best - self.worst)
            if 0.0 <= fraction <= 1.0:
                self.base_fraction = fraction
        self.update()

    def set_values(self, a: float, b: float) -> None:
        """Place the handles at two values in axis units, in either order,
        clamped onto the track. Silent (no moved) -- see the class note."""
        fa, fb = self.fraction_of(a), self.fraction_of(b)
        self.low_fraction, self.high_fraction = min(fa, fb), max(fa, fb)
        self.update()

    def values(self) -> tuple[float, float]:
        """The value at the left handle and at the right handle."""
        return self.value_at(self.low_fraction), self.value_at(self.high_fraction)

    def value_at(self, fraction: float) -> float:
        return self.worst + (self.best - self.worst) * fraction

    def fraction_of(self, value: float) -> float:
        span = self.best - self.worst
        if span == 0:
            # Degenerate axis (one fetched item, or none): the track has no
            # length to measure along, so anything at or below the value
            # is the left end and anything above is the right.
            return 0.0 if value <= self.worst else 1.0
        return min(1.0, max(0.0, (value - self.worst) / span))

    # ------------------------------------------------------------ interaction
    def _move(self, which: str, fraction: float) -> None:
        fraction = min(1.0, max(0.0, fraction))
        before = (self.low_fraction, self.high_fraction)
        if which == LEFT:
            self.low_fraction = min(fraction, self.high_fraction)
        else:
            self.high_fraction = max(fraction, self.low_fraction)
        self.active = which
        if (self.low_fraction, self.high_fraction) != before:
            self.update()
            self.moved.emit()

    def _track_span(self) -> tuple[int, int]:
        """Pixel x of fraction 0 and the pixel length of the track."""
        return self.INSET, max(self.width() - 2 * self.INSET, 1)

    def _fraction_at(self, x: float) -> float:
        start, length = self._track_span()
        return min(1.0, max(0.0, (x - start) / length))

    def _nearest(self, fraction: float) -> str:
        """Which handle a press means. Ties (both handles stacked) go to the
        side the press is on, so a collapsed pair can always be pulled apart."""
        d_low = abs(fraction - self.low_fraction)
        d_high = abs(fraction - self.high_fraction)
        if d_low == d_high:
            return LEFT if fraction < self.low_fraction else RIGHT
        return LEFT if d_low < d_high else RIGHT

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        fraction = self._fraction_at(event.position().x())
        self._dragging = self._nearest(fraction)
        self.setFocus(Qt.MouseFocusReason)
        self._move(self._dragging, fraction)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging is not None:
            self._move(self._dragging, self._fraction_at(event.position().x()))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._dragging = None

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        current = self.low_fraction if self.active == LEFT else self.high_fraction
        step = self.PAGE if event.modifiers() & Qt.ShiftModifier else self.STEP
        if key in (Qt.Key_Left, Qt.Key_Down):
            self._move(self.active, current - step)
        elif key in (Qt.Key_Right, Qt.Key_Up):
            self._move(self.active, current + step)
        elif key == Qt.Key_Home:
            self._move(self.active, 0.0)
        elif key == Qt.Key_End:
            self._move(self.active, 1.0)
        elif key == Qt.Key_Tab and self.active == LEFT:
            # Tab walks left -> right before leaving the widget, so both
            # handles are reachable without a mouse; from the right handle
            # it falls through to the normal focus chain.
            self.active = RIGHT
            self.update()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # ------------------------------------------------------------------ paint
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        pal = self.palette()
        start, length = self._track_span()
        track_y = (self.HEIGHT - self.TRACK_HEIGHT) // 2
        # Antialiasing stays off for the segments: the 14/1 rhythm reads as
        # crisp pixels or not at all.
        paint_segments(
            painter, start, track_y, length, self.TRACK_HEIGHT, palette.track_colour(pal)
        )

        x_low = start + round(length * self.low_fraction)
        x_high = start + round(length * self.high_fraction)
        # The selection wears the inspector's verdict colours, split at the
        # base tick: the part below the source module's value is red, the
        # part above it green, so a range reads the same way a roll does.
        # Without a base there is no side to be on and the accent stands in.
        if x_high > x_low:
            if self.base_fraction is None:
                accent = palette.RAIL_BAR[1] if palette.is_dark(pal) else palette.RAIL_BAR[0]
                spans = [(x_low, x_high, QColor(accent))]
            else:
                x_base = start + round(length * self.base_fraction)
                spans = [
                    (x_low, min(x_high, x_base), QColor(palette.delta_hex(False, pal))),
                    (max(x_low, x_base), x_high, QColor(palette.delta_hex(True, pal))),
                ]
            for x0, x1, colour in spans:
                if x1 <= x0:
                    continue
                painter.save()
                painter.setClipRect(x0, track_y, x1 - x0, self.TRACK_HEIGHT)
                paint_segments(painter, start, track_y, length, self.TRACK_HEIGHT, colour)
                painter.restore()

        ink = pal.color(QPalette.WindowText)
        if self.base_fraction is not None:
            x = start + round(length * self.base_fraction) - self.TICK_WIDTH // 2
            y = (self.HEIGHT - self.TICK_HEIGHT) // 2
            painter.fillRect(x, y, self.TICK_WIDTH, self.TICK_HEIGHT, ink)

        painter.setRenderHint(QPainter.Antialiasing)
        focused = self.hasFocus()
        for which, x in ((LEFT, x_low), (RIGHT, x_high)):
            ring = pal.color(
                QPalette.Highlight if focused and which == self.active else QPalette.Window
            )
            painter.setPen(QPen(ring, 1))
            painter.setBrush(ink)
            # The 1 px pen straddles the rect edge, so a rect half a pixel
            # outside the 8 x 14 handle paints the ring around it rather
            # than eating its outer pixel.
            rect = QRectF(
                x - self.HANDLE_WIDTH / 2 - 0.5, 0.5, self.HANDLE_WIDTH + 1, self.HANDLE_HEIGHT + 1
            )
            painter.drawRoundedRect(rect, self.HANDLE_RADIUS, self.HANDLE_RADIUS)


class _StatRow(QWidget):
    """One stat constraint: an attribute and a range in its display units.

    Three lines: the attribute combo and remove button; the worst-side
    field, the track and the best-side field; and under the track alone,
    the range's ends with the base between them. The fields sit beside the
    track in a 76 | track | 76 grid so the numbers stay next to the handle
    they bound.

    A fresh row starts with both handles on the ends. The range is the
    estate's own for this type, so the honest opening question is "every
    item of this type", narrowed as the handles move. Changing the
    attribute resets the range to full for the same reason: the fractions
    of one stat's range are no answer about another's.
    """

    changed = Signal()
    removed = Signal(object)

    def __init__(self, attrs: list[dict], bounds: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.attrs: list[dict] = []
        self.bounds: dict = {}
        self.decimals = 0
        self._shown_id: int | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.attr_combo = QComboBox()
        head.addWidget(self.attr_combo, 1)
        self.remove_btn = QToolButton()
        self.remove_btn.setText("×")
        self.remove_btn.setAutoRaise(True)
        self.remove_btn.setFixedSize(28, 28)
        self.remove_btn.setCursor(Qt.PointingHandCursor)
        self.remove_btn.setToolTip("Remove stat")
        self.remove_btn.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        head.addWidget(self.remove_btn)
        outer.addLayout(head)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(0)
        self.left_field = QLineEdit()
        self.right_field = QLineEdit()
        small = caption_font(self.font())
        for field in (self.left_field, self.right_field):
            field.setFixedSize(FIELD_WIDTH, FIELD_HEIGHT)
            field.setFont(small)
            field.installEventFilter(self)
        self.left_field.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.left_field.setToolTip("Worst-side bound")
        self.right_field.setToolTip("Best-side bound")
        self.track = _RangeTrack()
        grid.addWidget(self.left_field, 0, 0)
        grid.addWidget(self.track, 0, 1)
        grid.addWidget(self.right_field, 0, 2)
        grid.setColumnStretch(1, 1)

        ends = QHBoxLayout()
        ends.setContentsMargins(0, 0, 0, 0)
        ends.setSpacing(4)
        caption = caption_font(self.font())
        labels = []
        for alignment in (Qt.AlignLeft, Qt.AlignHCenter, Qt.AlignRight):
            label = QLabel("")
            label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
            label.setFont(caption)
            label.setAlignment(alignment | Qt.AlignVCenter)
            ends.addWidget(label, 1)
            labels.append(label)
        self.range_labels: tuple[QLabel, QLabel, QLabel] = tuple(labels)
        grid.addLayout(ends, 1, 1)
        outer.addLayout(grid)

        self.rebind(attrs, bounds)

        self.attr_combo.currentIndexChanged.connect(self._on_attribute_changed)
        self.track.moved.connect(self._on_track_moved)
        self.left_field.editingFinished.connect(lambda: self.commit_field(self.left_field))
        self.right_field.editingFinished.connect(lambda: self.commit_field(self.right_field))
        self.remove_btn.clicked.connect(lambda: self.removed.emit(self))

    # ---------------------------------------------------------------- state
    def attribute_id(self) -> int | None:
        data = self.attr_combo.currentData()
        return None if data is None else int(data)

    def attribute(self) -> dict | None:
        wanted = self.attribute_id()
        return next((a for a in self.attrs if int(a["attribute_id"]) == wanted), None)

    def rebind(self, attrs: list[dict], bounds: dict) -> None:
        """Take a (new) attribute list and bounds, keeping the current
        attribute selected when it is still offered. The handles keep their
        fractions across a rebind of the same attribute -- fresh bounds
        after a fetch move the ends, not the question."""
        keep = self.attribute_id()
        self.attrs = list(attrs)
        self.bounds = {int(k): v for k, v in bounds.items()}
        self.attr_combo.blockSignals(True)
        self.attr_combo.clear()
        for a in self.attrs:
            self.attr_combo.addItem(str(a["label"]), int(a["attribute_id"]))
        if keep is not None:
            index = self.attr_combo.findData(keep)
            if index >= 0:
                self.attr_combo.setCurrentIndex(index)
        self.attr_combo.blockSignals(False)
        self._on_attribute_changed(self.attr_combo.currentIndex())

    def select_attribute(self, attribute_id: int) -> bool:
        index = self.attr_combo.findData(int(attribute_id))
        if index < 0:
            return False
        self.attr_combo.setCurrentIndex(index)
        return True

    def set_used(self, attribute_ids: set[int]) -> None:
        """Grey out the attributes other rows already constrain. The row's
        own pick stays enabled, and a seeded duplicate (two chips on one
        stat, which the grammar allows) is still shown selected -- the
        entry is disabled for picking, not for being."""
        model = self.attr_combo.model()
        for index in range(self.attr_combo.count()):
            item = model.item(index)
            if item is not None:
                item.setEnabled(int(self.attr_combo.itemData(index)) not in attribute_ids)

    def _on_attribute_changed(self, _index: int) -> None:
        """Point the track and fields at the attribute's estate bounds,
        oriented worst to best by the attribute's display polarity."""
        attribute_id = self.attribute_id()
        lo, hi = self.bounds.get(attribute_id, (0.0, 0.0))
        attr = self.attribute() or {}
        self.decimals = _decimals(lo, hi)
        high = abyssal.display_high_is_good(attr.get("high_is_good"), attr.get("unit_id"))
        worst, best = (lo, hi) if high else (hi, lo)
        self.track.set_axis(worst, best, attr.get("base"))
        if attribute_id != self._shown_id:
            self._shown_id = attribute_id
            self.track.low_fraction, self.track.high_fraction = 0.0, 1.0
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        """Rewrite the fields and range labels from the track."""
        attr = self.attribute() or {}
        left, right = self.track.values()
        self.left_field.setText(_fmt(left, self.decimals, attr))
        self.right_field.setText(_fmt(right, self.decimals, attr))
        worst, base, best = self.range_labels
        worst.setText(_fmt(self.track.worst, self.decimals, attr))
        best.setText(_fmt(self.track.best, self.decimals, attr))
        base_value = attr.get("base")
        base.setText("" if base_value is None else f"base {_fmt(base_value, self.decimals, attr)}")

    def numeric_range(self) -> tuple[float, float]:
        """The selection as (low, high) in the stat's units, whichever way
        the track runs."""
        left, right = self.track.values()
        return min(left, right), max(left, right)

    def set_range(self, lo: float, hi: float) -> None:
        """Place the handles at two values in the stat's units, as a seed
        or a test does, and announce the change as a drag would."""
        self.track.set_values(lo, hi)
        self._refresh()
        self.changed.emit()

    # --------------------------------------------------------------- syncing
    def _on_track_moved(self) -> None:
        self._refresh()
        self.changed.emit()

    def commit_field(self, field: QLineEdit) -> None:
        """Take the typed number into the field's handle: clamped onto the
        axis, then to the other handle (a worst-side bound typed past the
        best-side one lands on it rather than swapping ends under the
        user). Garbage reverts to what the field showed, and a commit that
        moves nothing -- a blur off an untouched field -- announces
        nothing, so leaving a field does not ask for a fresh count."""
        value = _parse_number(field.text())
        if value is None:
            self._refresh()
            return
        before = (self.track.low_fraction, self.track.high_fraction)
        fraction = self.track.fraction_of(value)
        if field is self.left_field:
            self.track.low_fraction = min(fraction, self.track.high_fraction)
            self.track.active = LEFT
        else:
            self.track.high_fraction = max(fraction, self.track.low_fraction)
            self.track.active = RIGHT
        self.track.update()
        self._refresh()
        if (self.track.low_fraction, self.track.high_fraction) != before:
            self.changed.emit()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if (
            event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Return, Qt.Key_Enter)
            and isinstance(obj, QLineEdit)
        ):
            # Enter commits the field and goes no further: QLineEdit
            # ignores the key after acting on it, which would let it travel
            # up to the card, where Enter is Done.
            self.commit_field(obj)
            obj.selectAll()
            return True
        return super().eventFilter(obj, event)

    # ----------------------------------------------------------------- chips
    def _at_numeric_bounds(self) -> tuple[bool, bool]:
        """Whether the numerically low and high ends of the selection rest
        on the estate bounds -- read off the fractions, mapped through the
        axis orientation, since on a low-is-good row the numeric low end is
        the RIGHT handle."""
        open_left = self.track.low_fraction <= 0.0
        open_right = self.track.high_fraction >= 1.0
        if self.track.worst <= self.track.best:
            return open_left, open_right
        return open_right, open_left

    def chip(self) -> omni.Chip | None:
        """This row as a stat: chip, or None with no attribute to name. A
        handle resting on its end makes the comparison one-sided --
        ``cpu<=26`` rather than ``cpu=24..26`` -- because that is what the
        user dragged: one limit, not two. Both handles at their ends still
        emit the full range, so the row survives a Done/reopen round trip
        instead of vanishing.

        The bounds are the track's exact positions rounded outward (see
        _outward): the field's rounded number is what the user reads, but
        stat: compares the exact display value, and a chip written from the
        rounded number excluded the boundary items -- a full-range row over
        one BCS at 25.7999997 tf emitted ``CPU usage=25.8..25.8`` and
        emptied the table."""
        attr = self.attribute()
        if attr is None:
            return None
        name = attribute_chip_name(attr)
        exact_lo, exact_hi = self.numeric_range()
        lo = _number(_outward(exact_lo, self.decimals, up=False), self.decimals)
        hi = _number(_outward(exact_hi, self.decimals, up=True), self.decimals)
        at_low, at_high = self._at_numeric_bounds()
        if at_low and not at_high:
            value = f"{name}<={hi}"
        elif at_high and not at_low:
            value = f"{name}>={lo}"
        else:
            value = f"{name}={lo}..{hi}"
        return omni.Chip(omni.STAT_KIND, value)

    def seed_term(self, term) -> None:
        """Place the handles from a parsed chip. Strict operators land on
        the same handle as their inclusive twins: a track has no way to
        express "just above 26", and 26 is where the user will look for it."""
        lo_bound, hi_bound = sorted((self.track.worst, self.track.best))
        if term.op == "..":
            lo, hi = term.low, term.high if term.high is not None else hi_bound
        elif term.op in (">=", ">"):
            lo, hi = term.low, hi_bound
        else:
            lo, hi = lo_bound, term.low
        self.set_range(lo, hi)


class _TypeCompleter(QCompleter):
    """The type edit's completer: filters on _SEARCH_ROLE, completes to the label.

    QCompleter both matches and completes on one role. Matching wants the
    search text (label plus full name); completing wants the label, since
    the completion string is what Qt writes into the edit when an entry is
    highlighted with the arrow keys or activated. Left to the default, a
    Down in the popup put "Stasis Webifier · 12 Abyssal Stasis Webifier"
    in the edit. pathFromIndex is the one seam between the two, and Qt
    hands it the source-model index (confirmed against PySide6 6.11.2).
    """

    def pathFromIndex(self, index) -> str:  # noqa: N802
        return str(index.data(Qt.DisplayRole) or "")


class AbyssalCard(QFrame):
    """The popover. See the module docstring for the commit model."""

    selection_changed = Signal(list)   # [the selected type name], or [] for none
    filter_changed = Signal()          # the chips Done would write have changed
    fetch_requested = Signal(list)     # [the selected type name] ([] = every type)
    done = Signal(list)                # omni.Chip list to replace the card's kinds
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.Popup)
        self.setFixedWidth(CARD_WIDTH)
        self.setFrameShape(QFrame.StyledPanel)
        self.setObjectName("abyssalcard")
        self.setStyleSheet(
            "#abyssalcard { border: 1px solid palette(shadow); border-radius: 6px;"
            " background: palette(window); }"
        )
        self._attrs: list[dict] = []
        self._bounds: dict = {}
        self._rows: list[_StatRow] = []
        # Terms seeded before the attribute list arrived; bound into rows by
        # the first set_attributes that names their attribute.
        self._pending_terms: list[omni.StatTerm] = []
        self._seed_selected: list[str] = []
        self._applied = False
        small = caption_font(self.font())

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        head = QWidget()
        head_box = QVBoxLayout(head)
        head_box.setContentsMargins(12, 10, 12, 8)
        head_box.setSpacing(6)
        title = QLabel("Abyssal search")
        font = QFont(title.font())
        font.setWeight(QFont.Weight.DemiBold)
        font.setPointSizeF(font.pointSizeF() + 0.75)
        title.setFont(font)
        head_box.addWidget(title)
        types_hint = QLabel("Module type")
        types_hint.setFont(small)
        types_hint.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        head_box.addWidget(types_hint)
        self.type_combo = QComboBox()
        self.type_combo.setEditable(True)
        # The edit searches the list; it never adds to it (see the module
        # docstring), so a typed name that matches nothing must not become
        # an entry.
        self.type_combo.setInsertPolicy(QComboBox.NoInsert)
        tip = "Type a few letters of a module type, or pick one — empty is every abyssal item"
        self.type_combo.setToolTip(tip)
        self.type_edit = self.type_combo.lineEdit()
        self.type_edit.setToolTip(tip)
        self.type_edit.setPlaceholderText(TYPE_PLACEHOLDER)
        self._type_completer = _TypeCompleter(self.type_combo.model(), self.type_combo)
        self._type_completer.setCompletionMode(QCompleter.PopupCompletion)
        self._type_completer.setFilterMode(Qt.MatchContains)
        self._type_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._type_completer.setCompletionRole(_SEARCH_ROLE)
        self._type_completer.setMaxVisibleItems(COMPLETIONS_VISIBLE)
        # setCompleter wires the completer's activation to the combo, which
        # selects the entry by row -- the same path as a pick in the list,
        # so one selection fires currentIndexChanged exactly once.
        self.type_combo.setCompleter(self._type_completer)
        # Enter reaches the edit two ways: straight, when no completion popup
        # is up (the filter below takes it before the edit does), and via
        # the completer's popup, which hands the key to the edit directly
        # and so past any filter -- there returnPressed is the only hook.
        self.type_edit.installEventFilter(self)
        # Opened from the keyboard (F4) while completions are showing, the
        # arrow's list would stack on the completion popup and the two
        # would then take turns at the keys; the list showing dismisses
        # the completions (a mouse click on the arrow already does, since
        # the completer eats the click that lands outside its popup).
        self.type_combo.view().installEventFilter(self)
        self.type_edit.returnPressed.connect(self._commit_type_text)
        self.type_edit.editingFinished.connect(self._settle_type_text)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        head_box.addWidget(self.type_combo)

        self.banner = QWidget()
        banner_row = QHBoxLayout(self.banner)
        banner_row.setContentsMargins(0, 0, 0, 0)
        self.banner_label = QLabel("")
        banner_row.addWidget(self.banner_label, 1)
        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.setToolTip("Ask ESI for the rolls of the items not fetched yet")
        self.fetch_btn.clicked.connect(self._on_fetch)
        banner_row.addWidget(self.fetch_btn)
        self.banner.setVisible(False)
        head_box.addWidget(self.banner)
        root.addWidget(head)

        body = QWidget()
        body_box = QVBoxLayout(body)
        body_box.setContentsMargins(12, 6, 12, 10)
        body_box.setSpacing(12)
        self.rows_hint = QLabel("Pick a module type to filter on its rolled stats.")
        self.rows_hint.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        body_box.addWidget(self.rows_hint)
        self.rows_box = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_box)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(12)
        body_box.addWidget(self.rows_box)
        self.add_row_btn = QToolButton()
        self.add_row_btn.setText("+ Add stat")
        self.add_row_btn.setAutoRaise(True)
        self.add_row_btn.setFixedHeight(24)
        self.add_row_btn.setCursor(Qt.PointingHandCursor)
        self.add_row_btn.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.add_row_btn.clicked.connect(lambda: self.add_row())
        body_box.addWidget(self.add_row_btn, 0, Qt.AlignLeft)
        root.addWidget(body)

        # The footer sits a step above the window with a rule over it, both
        # derived from the palette (toward_text) so they exist on every
        # theme; the bottom corners follow the card's radius so the surface
        # does not poke square corners out of the rounded border.
        pal = self.palette()
        self.footer = QFrame()
        self.footer.setObjectName("abyssalfooter")
        self.footer.setStyleSheet(
            "#abyssalfooter {"
            f" background: {palette.toward_text(pal, 0.05).name()};"
            f" border-top: 1px solid {palette.track_colour(pal).name()};"
            " border-bottom-left-radius: 5px; border-bottom-right-radius: 5px; }"
        )
        foot = QHBoxLayout(self.footer)
        foot.setContentsMargins(12, 8, 12, 8)
        foot.setSpacing(8)
        self.match_count_label = QLabel("…")
        self.match_count_label.setFont(small)
        self.match_rest_label = QLabel("")
        self.match_rest_label.setFont(small)
        self.match_rest_label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        # One sentence in two colours: the pair sits at word spacing, not
        # the footer's button spacing.
        sentence = QHBoxLayout()
        sentence.setSpacing(3)
        sentence.addWidget(self.match_count_label)
        sentence.addWidget(self.match_rest_label, 1)
        foot.addLayout(sentence, 1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.hide)
        foot.addWidget(self.cancel_btn)
        self.done_btn = QPushButton("Done")
        self.done_btn.setToolTip("Apply (Enter)")
        self.done_btn.clicked.connect(self.apply)
        foot.addWidget(self.done_btn)
        root.addWidget(self.footer)

        self._update_rows_enabled()

    # ------------------------------------------------------------------- API
    def seed(self, chips: list) -> None:
        """Start over from the omnibox's current chips: the abyssal chip's
        first type becomes the dropdown's selection (see the module
        docstring for why a chip naming several is cut to one), and every
        positive stat: chip becomes a row as soon as the attribute list
        names its attribute. A roll: chip is not the card's to edit and is
        left where it is. The previous attribute list is dropped too -- it
        belonged to whatever type the card last showed. Silent: the view
        fetches the seeded type's data straight after, and that fetch
        announces the filter once the rows exist."""
        self._attrs = []
        self._bounds = {}
        self._pending_terms = []
        for row in list(self._rows):
            self._remove_row(row, announce=False)
        selected: list[str] = []
        for chip in chips:
            if chip.negated:
                continue
            if chip.kind == omni.ABYSSAL_KIND:
                selected.extend(t for t in omni.split_types(chip.value) if t not in selected)
            elif chip.kind == omni.STAT_KIND:
                term = omni.parse_stat(chip.value)
                if term is not None:
                    self._pending_terms.append(term)
        self._seed_selected = selected[:1]
        # The view's set_types follows every seed, but the card is shown
        # before that fetch lands and must not flash last time's pick. A
        # seeded type is only worth listing at 0 over a list that exists;
        # with none yet, the fresh list selects it on arrival.
        if self._seed_selected and self.type_combo.count():
            self._select_type(self._seed_selected[0])
        else:
            self._select_type(None)
        self._update_rows_enabled()
        self._set_counting()

    def seeded_types(self) -> list[str]:
        """The type the seed selected, as a one-element list, or [] for none
        -- the shape the view's fetch and set_types take."""
        return list(self._seed_selected)

    def set_types(self, rows, selected: list[str]) -> None:
        """Fill the dropdown with one entry per (type_id, name, items,
        fetched) row, in the rows' order, selecting the first of the
        selected names (none leaves no entry selected and the edit empty).
        A selected name the estate no longer holds (a saved view outliving
        its item) is listed with a count of 0 so the user can see it and
        pick another, rather than filtering to an empty table for no
        visible reason. Silent: the view already knows what it seeded, so
        no selection_changed."""
        self.type_combo.blockSignals(True)
        try:
            self.type_combo.clear()
            seen: set[str] = set()
            entries: list[tuple[str, int]] = []
            for row in rows:
                name, count = str(row["name"]), int(row["items"] or 0)
                seen.add(name)
                entries.append((name, count))
            for name in selected:
                if name not in seen:
                    entries.append((name, 0))
            for name, count in entries:
                self._add_type_entry(name, count)
            self._select_type(selected[0] if selected else None)
        finally:
            self.type_combo.blockSignals(False)
        self._update_rows_enabled()
        if self.isVisible() and self.type_edit.hasFocus():
            # The list usually lands after the card is shown with the edit
            # focused; the selection wrote its label into the edit, and the
            # user about to type must replace it, not append to it. (A
            # hidden popup's edit still reports focus, hence the first test.)
            self.type_edit.selectAll()

    def _add_type_entry(self, name: str, count: int) -> None:
        label = f"{abyssal.strip_type_prefix(name)} · {count:,}"
        self.type_combo.addItem(label, name)
        last = self.type_combo.count() - 1
        self.type_combo.setItemData(last, name, Qt.ToolTipRole)
        self.type_combo.setItemData(last, f"{label} {name}", _SEARCH_ROLE)

    def _select_type(self, name: str | None) -> None:
        """Select a type's entry -- or, for None, no entry -- without
        announcing it, listing the name with a count of 0 first if the
        dropdown does not offer it."""
        index = -1
        if name is not None:
            index = self.type_combo.findData(name)
            if index < 0:
                self._add_type_entry(name, 0)
                index = self.type_combo.count() - 1
        self.type_combo.blockSignals(True)
        try:
            self._set_type_index(index)
        finally:
            self.type_combo.blockSignals(False)

    def _set_type_index(self, index: int) -> None:
        """setCurrentIndex, with the edit emptied for -1. Qt writes the new
        entry's label into the edit when the index moves, and for -1 that
        label is empty; but an index that does not move leaves whatever
        text was standing, and the deselect that matters most -- the edit
        emptied by the user, then emptied again -- must still end with the
        placeholder showing rather than a stray space."""
        self.type_combo.setCurrentIndex(index)
        if index < 0 and self.type_edit.text():
            self.type_edit.clear()

    def selected_types(self) -> list[str]:
        """The selected type as a one-element list, or [] for none -- the
        list shape the chip value and the view's fetch are built from."""
        name = self.type_combo.currentData()
        return [str(name)] if name else []

    def set_attributes(self, attrs: list[dict], bounds: dict) -> None:
        """The rolled attributes and value bounds of the one selected type
        (queries.abyssal_type_columns' pair: attributes the estate holds
        values for, each with its polarity and base, and their bounds). An
        attribute without bounds is not offered at all -- a track has
        nothing to run between -- so a stale list that still names one
        cannot put a (0, 0) row on the card. Rows whose attribute the new
        list still rolls are kept and rebound; the others are dropped,
        since a row about a stat this type does not have could only emit a
        chip matching nothing. An empty list (no type, or several, or one
        type with nothing fetched yet) leaves the rows alone but disabled
        -- clearing the type and picking it again must not cost the rows
        built for it. Always announces the filter: this is the point
        at which the rows Done would write are known, and the live count
        is stale until it is asked again."""
        self._bounds = {int(k): v for k, v in bounds.items()}
        self._attrs = [a for a in attrs if int(a["attribute_id"]) in self._bounds]
        if self._attrs:
            offered = {int(a["attribute_id"]) for a in self._attrs}
            for row in list(self._rows):
                if row.attribute_id() in offered:
                    row.rebind(self._attrs, self._bounds)
                else:
                    self._remove_row(row, announce=False)
            pending, self._pending_terms = self._pending_terms, []
            for term in pending:
                self._add_row_for_term(term)
        self._update_rows_enabled()
        self._announce()

    def set_pending(self, count: int) -> None:
        """The not-fetched banner: hidden at zero, otherwise the count and a
        Fetch button. Re-armed on every call so a second look at the card
        after a fetch reflects the new count."""
        self.banner.setVisible(count > 0)
        plural = "s" if count != 1 else ""
        self.banner_label.setText(f"{count:,} abyssal item{plural} not fetched —")
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch")

    def set_match_count(self, matched: int, total: int) -> None:
        """The footer's live answer: how many items the chips Done would
        write match, out of the picked type's items (or every abyssal item
        with no type picked). The count wears the primary text colour and the rest
        the muted one, so the figure is what the eye lands on."""
        self.match_count_label.setText(f"{matched:,}")
        self.match_rest_label.setText(f"of {total:,} match")

    def add_row(self, attribute_id: int | None = None) -> _StatRow | None:
        """Append a row on attribute_id, or on the first attribute no row
        constrains yet -- None when there is no attribute list, or (with
        none asked for) none left unused. An explicit id is honoured even
        when another row already has it: the grammar allows two chips on
        one stat and the seed must reproduce them."""
        if not self._attrs:
            return None
        if attribute_id is None:
            used = {row.attribute_id() for row in self._rows}
            attribute_id = next(
                (int(a["attribute_id"]) for a in self._attrs if int(a["attribute_id"]) not in used),
                None,
            )
            if attribute_id is None:
                return None
        row = _StatRow(self._attrs, self._bounds)
        row.select_attribute(attribute_id)
        row.removed.connect(self._remove_row)
        row.changed.connect(self._on_row_changed)
        self._rows.append(row)
        self.rows_layout.addWidget(row)
        self._refresh_usage()
        self.adjustSize()
        self._announce()
        return row

    def rows(self) -> list[_StatRow]:
        return list(self._rows)

    def chips(self) -> list[omni.Chip]:
        """What Done hands back: the abyssal chip (its value the selected
        type, empty with none) plus one chip per row while a type is
        selected. No type is still a valid answer -- "every abyssal item" --
        and emits the plain chip; the rows, which belong to no type then,
        are dropped."""
        types = self.selected_types()
        out = [omni.Chip(omni.ABYSSAL_KIND, omni.join_types(types))]
        if types:
            for row in self._rows:
                chip = row.chip()
                if chip is not None:
                    out.append(chip)
        return out

    def apply(self) -> None:
        """Done: hide first so the hide reads as applied, not cancelled, then
        hand the chips over -- by then the popup is gone and the view's
        set_spec reload paints under nothing."""
        chips = self.chips()
        self._applied = True
        self.hide()
        self.done.emit(chips)

    # ------------------------------------------------------------- internals
    def _add_row_for_term(self, term: omni.StatTerm) -> None:
        # The offered list already excludes attributes without bounds, so a
        # chip naming one (typed, or saved before a re-fetch) is dropped the
        # same way as a chip about a stat this type does not roll -- forcing
        # it onto (0, 0) bounds once re-emitted it as ``name=0..0``, a chip
        # about nothing.
        attr = next((a for a in self._attrs if _matches(a, term.name)), None)
        if attr is None:
            return
        row = self.add_row(int(attr["attribute_id"]))
        if row is not None:
            row.seed_term(term)

    def _remove_row(self, row: _StatRow, announce: bool = True) -> None:
        if row not in self._rows:
            return
        self._rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._refresh_usage()
        self.adjustSize()
        if announce:
            self._announce()

    def _on_row_changed(self) -> None:
        self._refresh_usage()
        self._announce()

    def _refresh_usage(self) -> None:
        """Every row greys out the attributes the other rows hold, and Add
        stat follows (see _update_rows_enabled)."""
        used = {row.attribute_id() for row in self._rows}
        for row in self._rows:
            row.set_used(used - {row.attribute_id()})
        self._update_rows_enabled()

    def _announce(self) -> None:
        """The chips Done would write have changed: show the count as
        pending and ask the view for a fresh one."""
        self._set_counting()
        self.filter_changed.emit()

    def _set_counting(self) -> None:
        self.match_count_label.setText("…")

    def _on_type_changed(self, _index: int) -> None:
        self._update_rows_enabled()
        self.selection_changed.emit(self.selected_types())
        self._announce()

    # ----------------------------------------------------------- type search
    def _commit_type_text(self) -> None:
        """Enter in the type edit: select the entry the text means, or put
        the current entry's label back. Never Done -- Enter here answers
        the question the edit asked, and a search that applied the filter
        on the same keystroke would commit whatever the first match was.
        The text is left selected so the next keystroke starts a fresh
        search, one turn later because Qt's own activation tail (the
        completer writing the label into the edit) runs after this slot
        and would clear a selection made now."""
        index = self._type_row_for(self.type_edit.text())
        if index >= 0:
            self.type_combo.setCurrentIndex(index)
        self._settle_type_text()
        popup = self._type_completer.popup()
        if popup is not None and popup.isVisible():
            popup.hide()
        QTimer.singleShot(0, self.type_edit.selectAll)

    def _type_row_for(self, text: str) -> int:
        """The entry a typed text means, or -1: an entry's exact label or
        exact full name first, then the completion the user highlighted,
        then the first entry whose search text contains the typed text --
        the same order and test the completer's popup shows, so Enter takes
        the top of the list the user is looking at. The scan repeats the
        completer's test rather than reading its model because the
        completer knows only the text typed or pasted since it last
        filtered -- not a label a selection wrote into the edit -- and
        highlights no row until Down; one pass over the entries answers
        both."""
        needle = text.strip().lower()
        if not needle:
            return -1
        combo = self.type_combo
        for index in range(combo.count()):
            if combo.itemText(index).lower() == needle:
                return index
            if str(combo.itemData(index) or "").lower() == needle:
                return index
        popup = self._type_completer.popup()
        current = popup.currentIndex() if popup is not None and popup.isVisible() else None
        if current is not None and current.isValid():
            highlighted = current.data(_SEARCH_ROLE)
            for index in range(combo.count()):
                if combo.itemData(index, _SEARCH_ROLE) == highlighted:
                    return index
        for index in range(combo.count()):
            if needle in str(combo.itemData(index, _SEARCH_ROLE) or "").lower():
                return index
        return -1

    def _settle_type_text(self) -> None:
        """Make the edit and the selection agree once the typing is over,
        on Enter and on leaving the edit. An emptied edit deselects the
        type: it is the one way from a type back to every abyssal item, and
        the user who cleared the field meant "nothing", not "what it said
        before". Any other text that matched no entry reverts to the
        selected entry's label, since Qt leaves an unmatched text standing
        in an editable combo and an edit reading "zzz" over a selection of
        the webifier would be lying about the filter. The deselect is
        announced the way a pick is, through currentIndexChanged, and only
        when it is a change."""
        if not self.type_edit.text().strip():
            self._set_type_index(-1)
            return
        label = self.type_combo.itemText(self.type_combo.currentIndex())
        if self.type_edit.text() != label:
            self.type_edit.setText(label)

    def _update_rows_enabled(self) -> None:
        """The body follows the pick. With no type the rows go dark under
        the hint and Add stat is gone -- a row it added would be about no
        type. With one, the button is back: disabled until the attribute
        list lands, gone again once every attribute has a row, since there
        is nothing left it could add."""
        one = bool(self.selected_types())
        self.rows_box.setEnabled(one)
        self.rows_hint.setVisible(not one)
        used = {row.attribute_id() for row in self._rows}
        exhausted = bool(self._attrs) and all(
            int(a["attribute_id"]) in used for a in self._attrs
        )
        self.add_row_btn.setVisible(one and not exhausted)
        self.add_row_btn.setEnabled(one and bool(self._attrs))

    def _on_fetch(self) -> None:
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Fetching…")
        self.fetch_requested.emit(self.selected_types())

    # ---------------------------------------------------------------- events
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.type_combo.view() and event.type() == QEvent.Show:
            popup = self._type_completer.popup()
            if popup is not None and popup.isVisible():
                popup.hide()
            return False
        if obj is self.type_edit and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                # Taken before the edit sees it: left to travel, the edit
                # ignores the key and it climbs to the card, where Enter is
                # Done.
                self._commit_type_text()
                return True
            if key == Qt.Key_Escape:
                # A completion popup takes the first Escape (Qt's own
                # popup filter does the same when the key comes in through
                # the popup); only a second one cancels the card.
                popup = self._type_completer.popup()
                if popup is not None and popup.isVisible():
                    popup.hide()
                else:
                    self.hide()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.hide()
            event.accept()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            # Enter anywhere in the card but an edit (a bound field's row
            # commits it, the type edit's filter selects on it) is Done: the
            # buttons ignore the key, so it arrives here.
            self.apply()
            event.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        self._applied = False
        super().showEvent(event)
        # The card opens ready to type a type: focus on the edit with its
        # text selected, so the first keystroke starts the search rather
        # than appending to the current label. Focus inside a Qt.Popup is
        # ordinary -- the popup owns the keyboard while it is up -- and
        # openPopup hands the keyboard to the popup's focus widget, which
        # this makes the edit.
        self.type_edit.setFocus()
        self.type_edit.selectAll()

    def hideEvent(self, event) -> None:  # noqa: N802
        """Every way the popup goes away that is not Done -- Cancel, Esc, an
        outside click closing the popup -- ends here and reads as Cancel."""
        super().hideEvent(event)
        if not self._applied:
            self.cancelled.emit()
