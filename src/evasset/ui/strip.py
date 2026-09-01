"""The estate strip: whole-estate headline figures above the assets table.

The strip is deliberately independent of the table's filters -- it answers
"what is everything worth" while the table underneath answers "what am I
looking at", so adding a chip must never make the headline number jump. The
matching query-side decision is documented on queries.estate_summary.

The widget itself is dumb on purpose: the host fetches estate_summary() and
value_map() through its own AsyncQuery and hands the results to set_data().
Keeping the DB out of here means the strip renders identically from a live
query and from a hand-built dict in a test, and there is exactly one place
(the host's reload path) deciding when estate figures are stale.

Rendering follows the concept board's hierarchy rather than a flat label
row: a small uppercase caption over a large value with its unit in muted
text, one evenly stretched cell per figure with hairlines between them, the
liquid figure in the gain green and the unpriced count in warning amber --
the two numbers that mean something at a glance mean it in colour too.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import palette
from .models import fmt_short_isk

# Lightness factors cycled across the value-map segments so adjacent segments
# stay distinguishable without adding new colour pairs to palette.py -- every
# segment is the vetted RAIL_BAR hue, only lighter or darker. A filled bar is
# a graphical object (WCAG 1.4.11, 3:1 floor), not text, and the base pair
# already passes that in tests/test_contrast.py.
_SEGMENT_LIGHTNESS = (100, 135, 75, 160, 90, 120)


def _caption_label(text: str) -> QLabel:
    """The concept board's cell caption: small, uppercase, letter-spaced,
    muted -- scaffolding the eye skips once the layout is familiar."""
    label = QLabel(text.upper())
    label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
    font = label.font()
    font.setPointSizeF(max(font.pointSizeF() - 1.5, 6.0))
    font.setLetterSpacing(QFont.PercentageSpacing, 106)
    label.setFont(font)
    return label


class _StatCell(QWidget):
    """Caption over a large value with an optional muted unit suffix.

    The size step between caption and value is the whole point: the flat
    first version set both in the default size and the strip read as two
    rows of noise instead of five figures.
    """

    def __init__(self, caption: str, unit: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(10, 4, 10, 4)
        box.setSpacing(1)
        box.addWidget(_caption_label(caption))

        row = QHBoxLayout()
        row.setSpacing(4)
        self.value = QLabel("—")
        font = self.value.font()
        font.setPointSizeF(font.pointSizeF() + 2.5)
        self.value.setFont(font)
        row.addWidget(self.value)
        self.unit = QLabel(unit)
        self.unit.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.unit.setVisible(bool(unit))
        row.addWidget(self.unit)
        row.addStretch(1)
        box.addLayout(row)
        self._row = row

    def set_value(self, text: str, colour: str | None = None, tooltip: str = "") -> None:
        self.value.setText(text)
        self.value.setStyleSheet(f"color: {colour};" if colour else "")
        self.setToolTip(tooltip)


def _rule() -> QFrame:
    """Hairline between cells. QFrame draws its lines with the theme's own
    frame roles, so this follows light/dark with nothing to maintain."""
    line = QFrame()
    line.setFrameShape(QFrame.VLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


# A value-map segment narrower than this is unreadable, untargetable with a
# mouse, and pure paint/hit-test overhead -- it gets folded into the residue.
_MIN_SEGMENT_PX = 4


class _ValueMap(QWidget):
    """One-row bar of the top locations by sell value, widths proportional.

    Culling is dynamic to the widget's width: any segment that would paint
    narrower than _MIN_SEGMENT_PX folds into one muted "N more" residue
    segment at the tail (the concept board's "14 more…" cell), so however
    many segments the query supplies, the work per paint, tooltip and click
    is bounded by width / _MIN_SEGMENT_PX + 1. The values arrive sorted
    descending, so the fold is a single suffix. Geometry is computed once
    per (segments, width) pair and cached; a resize or new data invalidates
    it, which is what makes the culling dynamic rather than baked in at
    set_segments time.

    An empty map paints a subdued track rather than nothing -- an invisible
    widget reads as broken, an empty track reads as "no priced assets yet"
    (which is also what its tooltip says in that state).
    """

    segment_clicked = Signal(str)  # location label

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._segments: list[tuple[str, float]] = []
        # (width the spans were computed for, spans, residue count)
        self._cache: tuple[int, list, int] | None = None
        self.setFixedHeight(18)
        self.setMinimumWidth(120)
        self.setCursor(Qt.PointingHandCursor)

    def set_segments(self, segments) -> None:
        """Replace the segments (queries.value_map shape: label, sell_value).
        Zero-value rows are dropped -- a zero-width segment can neither be
        seen nor clicked."""
        cleaned = [(str(s["label"]), float(s["sell_value"] or 0)) for s in segments]
        self._segments = [(label, value) for label, value in cleaned if value > 0]
        self._cache = None
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._cache = None
        super().resizeEvent(event)

    def _spans(self) -> list[tuple[str | None, float, float, float]]:
        """(label, value, x0, x1) per visible segment across the current
        width; label None marks the residue of culled segments."""
        if self._cache is not None and self._cache[0] == self.width():
            return self._cache[1]
        total = sum(value for _label, value in self._segments)
        spans: list[tuple[str | None, float, float, float]] = []
        culled_value = 0.0
        culled = 0
        if total > 0:
            x = 0.0
            for label, value in self._segments:
                width = self.width() * value / total
                if width < _MIN_SEGMENT_PX:
                    culled_value += value
                    culled += 1
                    continue
                spans.append((label, value, x, x + width))
                x += width
            if culled:
                # The residue keeps its true share of the bar, however thin
                # -- widening it would misstate every other segment's share.
                spans.append((None, culled_value, x, self.width()))
        self._cache = (self.width(), spans, culled)
        return spans

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(0, 0, self.width(), self.height(), 3, 3)
        painter.setClipPath(clip)
        painter.fillRect(self.rect(), self.palette().alternateBase())
        spans = self._spans()
        if not spans:
            return
        pair = palette.RAIL_BAR
        base = QColor(pair[1] if palette.is_dark(self.palette()) else pair[0])
        for i, (label, _value, x0, x1) in enumerate(spans):
            if label is None:
                # The culled-segments residue is deliberately muted: it is
                # "everything too small to matter here", not a location.
                colour = self.palette().color(QPalette.Dark)
            else:
                colour = QColor(base).lighter(_SEGMENT_LIGHTNESS[i % len(_SEGMENT_LIGHTNESS)])
            # The 1 px gap keeps adjacent segments countable even where the
            # lightness cycle happens to bring two close together.
            painter.fillRect(
                round(x0), 0, max(round(x1 - x0) - 1, 1), self.height(), colour
            )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            # Every other click-to-chip target in the tab is left-click only;
            # a right-click here must not silently add a filter.
            return
        x = event.position().x()
        for label, _value, x0, x1 in self._spans():
            if x0 <= x < x1:
                # The residue is many locations at once -- there is no single
                # chip a click on it could honestly add.
                if label is not None:
                    self.segment_clicked.emit(label)
                return

    def event(self, ev) -> bool:
        if ev.type() == QEvent.ToolTip:
            for label, value, x0, x1 in self._spans():
                if x0 <= ev.pos().x() < x1:
                    if label is None:
                        culled = self._cache[2] if self._cache else 0
                        text = f"{culled} more location(s) · {fmt_short_isk(value)} ISK"
                    else:
                        text = f"{label} · {fmt_short_isk(value)} ISK"
                    QToolTip.showText(ev.globalPos(), text, self)
                    return True
            if not self._segments:
                QToolTip.showText(ev.globalPos(), "No priced assets yet", self)
                return True
            QToolTip.hideText()
            return True
        return super().event(ev)


class EstateStrip(QWidget):
    """Six cells in a row: net worth, assets, liquid ISK, volume, unpriced
    count with a "show" badge, and the value map."""

    unpriced_clicked = Signal()
    location_clicked = Signal(str)  # value-map segment label

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # The strip reads as one band, so it carries its own top and bottom
        # hairlines rather than leaning on whatever neighbour happens to sit
        # above or below it in the tab.
        self.setObjectName("estatestrip")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "#estatestrip { border-top: 1px solid palette(dark);"
            " border-bottom: 1px solid palette(dark); }"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(2, 1, 6, 1)
        row.setSpacing(0)

        # Stat cells sit at their natural width, packed left; all leftover
        # width goes to the value map (its usefulness grows with every
        # pixel, a number's does not), which lands it at roughly half the
        # window on a typical size and more on a wide one.
        self._net_worth = _StatCell("Net worth", unit="ISK")
        self._assets = _StatCell("Assets (est.)")
        self._liquid = _StatCell("Liquid ISK")
        self._volume = _StatCell("Volume", unit="m³")
        for cell in (self._net_worth, self._assets, self._liquid, self._volume):
            row.addWidget(cell, 0)
            row.addWidget(_rule())

        unpriced_cell = _StatCell("Unpriced")
        self._unpriced = unpriced_cell
        self.unpriced_btn = QToolButton()
        self.unpriced_btn.setText("show")
        self.unpriced_btn.setCursor(Qt.PointingHandCursor)
        self.unpriced_btn.setToolTip("Filter the table to unpriced stacks")
        self.unpriced_btn.setVisible(False)  # nothing to show until set_data says so
        self.unpriced_btn.clicked.connect(self.unpriced_clicked)
        # Insert the badge before the row's trailing stretch so it hugs the
        # count the way the concept board draws it.
        unpriced_cell._row.insertWidget(unpriced_cell._row.count() - 1, self.unpriced_btn)
        row.addWidget(unpriced_cell, 0)
        row.addWidget(_rule())

        map_cell = QWidget()
        map_box = QVBoxLayout(map_cell)
        map_box.setContentsMargins(10, 4, 10, 4)
        map_box.setSpacing(3)
        map_box.addWidget(_caption_label("Value map"))
        self.value_map = _ValueMap()
        self.value_map.segment_clicked.connect(self.location_clicked)
        map_box.addWidget(self.value_map)
        # The one stretching cell -- see the layout comment above the stat
        # cells for why the map gets everything the numbers do not need.
        row.addWidget(map_cell, 1)

        # Kept as attributes the tests and set_data address by name.
        self.net_worth = self._net_worth.value
        self.assets = self._assets.value
        self.liquid = self._liquid.value
        self.volume = self._volume.value
        self.unpriced = self._unpriced.value

    def set_data(self, summary: dict, segments: list) -> None:
        """Render an estate summary (queries.estate_summary shape) and the
        value-map segments (queries.value_map shape)."""
        self._net_worth.set_value(
            fmt_short_isk(summary["total"]),
            tooltip=f"{summary['total']:,.2f} ISK at Jita sell",
        )
        self._assets.set_value(fmt_short_isk(summary["assets_sell"]))
        # Liquid is the "spendable now" figure, so it borrows the net-worth
        # chart's gain green; unpriced is the honesty figure and borrows the
        # warning amber -- the same measured pairs every other view uses.
        self._liquid.set_value(
            fmt_short_isk(summary["wallet_liquid"]),
            colour=palette.delta_hex(True, self.palette()),
        )
        self._volume.set_value(
            fmt_short_isk(summary["volume"]),
            tooltip=f"{summary['volume']:,.0f} m³",
        )
        unpriced = int(summary["unpriced_stacks"])
        warn = palette.status_hex(palette.WARN, self.palette())
        self._unpriced.set_value(f"{unpriced:,}", colour=warn if unpriced else None)
        # The badge is a filled pill in the warning colour; the text inverts
        # against it (the light theme's amber is dark, the dark theme's is
        # bright), a pairing pinned in tests/test_contrast.py.
        text_on_warn = "#000000" if palette.is_dark(self.palette()) else "#FFFFFF"
        self.unpriced_btn.setStyleSheet(
            f"QToolButton {{ background: {warn}; color: {text_on_warn}; border: none;"
            " border-radius: 8px; padding: 1px 8px; font-weight: 600; }"
        )
        # The badge is the click target for "filter to unpriced"; with zero
        # unpriced stacks that filter shows an empty table, so hide it.
        self.unpriced_btn.setVisible(unpriced > 0)
        self.value_map.set_segments(segments)
