"""Row inspector: full detail for one asset row, in the panel or in a window.

The Inspector widget has two hosts. A click (or Enter) on a row shows it in
the panel that slides over the rail in the splitter's second slot: the rail
and a quick look at the current row answer the same "tell me more about what
I selected" need, only one is useful at a time, and sharing the slot means
the panel inherits the rail's width and resize behaviour for free (the host
swaps them in a QStackedWidget). Right-click -> Inspect in window puts a
second, independent Inspector in InspectorWindow, a separate top-level window
the way the fit tool opens: it pins one item while the panel follows the
clicks -- which is how two mutated modules get compared roll by roll -- and
it keeps the rail in view while "Where else?" flips it to per-station counts.
The window is shown, not exec'd, because those actions change the table
behind it, which a modal loop would block.

Deliberately dumb, like EstateStrip: show_row() renders whatever row it is
handed and every button only emits a signal. The host owns the database
writes, the network job and the dialogs those buttons imply, so this widget
tests as pure rendering and the threading rules (async_query.py) stay the
host's single responsibility.

The location line shows the resolved root location plus the slot flag. A
recursive location_id walk reconstructing the full container nesting ("in a
can, in a ship, in a hangar") was considered and left out: ASSET_ROWS already
flattens every row to its root location, the flag names the immediate
context, and the walk would need its own query per opened row for a path the
table's Location column cannot show anyway.

Abyssal (mutated) modules get one extra section: the mutator's rolled
attributes, one _RollRow each -- the attribute's name against its rolled
value and signed delta, a segmented meter running from the mutator's worst
possible roll to its best with the source module's base ticked and the
base-to-roll span filled in the verdict's colour, and the range's ends
labelled underneath. The section is driven by show_rolls() with a payload
the host fetched (queries.fetch_abyssal_rolls) -- the same rule as
show_row, so the rolls block tests from a hand-built dict
without a database.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import abyssal
from . import palette

# One tolerant ISO parser for price timestamps; importing grouped_model's
# beats a second copy of the 'Z'-suffix workaround drifting out of step.
# _row_get likewise: an ASSET_ROWS column the row happens to lack (a test
# SELECT, a saved query shape from before the column existed) must read as
# absent, not raise mid-render.
from .grouped_model import _parse_utc, _row_get
from .meters import paint_segments
from .models import fmt_isk, fmt_num, fmt_short_isk

# The estate strip's section caption, so "ROLLED STATS" is set exactly like
# "VALUE MAP" rather than a second small-caps recipe drifting from the first.
from .strip import _caption_label, caption_font

# ASSET_ROWS carries the meta group's name, not its id, so the pill maps the
# four rarity names back to palette.RARITY_TINTS' keys -- "Faction" reads
# green here exactly as it does on a fitted module in the fit dialog; every
# other meta group keeps the neutral accent.
_RARITY_META_IDS = {
    "Faction": palette.META_FACTION,
    "Officer": palette.META_OFFICER,
    "Deadspace": palette.META_DEADSPACE,
    "Abyssal": palette.META_ABYSSAL,
}


class Inspector(QWidget):
    """Detail panel for one row; every action is a signal the host handles."""

    close_clicked = Signal()
    where_else_clicked = Signal()
    refresh_price_clicked = Signal()
    pin_price_clicked = Signal()
    fetch_abyssal_clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # Same floor reasoning as the rail: without an explicit minimum the
        # form layout's widest row decides how narrow the host can go.
        self.setMinimumWidth(150)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)

        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        self.title = QLabel("")
        self.title.setStyleSheet("font-weight: 600;")
        self.title.setWordWrap(True)
        title_box.addWidget(self.title)
        self.subtitle = QLabel("")
        self.subtitle.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.subtitle.setWordWrap(True)
        title_box.addWidget(self.subtitle)
        head.addLayout(title_box, 1)
        self.close_btn = QToolButton()
        self.close_btn.setText("×")
        self.close_btn.setAutoRaise(True)
        self.close_btn.setToolTip("Close (Esc)")
        self.close_btn.clicked.connect(self.close_clicked)
        head.addWidget(self.close_btn, 0, Qt.AlignTop)
        root.addLayout(head)

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(2)
        self.owner = QLabel("")
        self.meta = QLabel("")
        # Restyled per row in show_row: the pill borrows the fit dialog's
        # rarity wash where the meta group has one, so "Faction" reads green
        # here exactly as it does on a fitted module.
        self.location = QLabel("")
        self.location.setWordWrap(True)
        self.place = QLabel("")
        self.place.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        self.quantity = QLabel("")
        self.volume = QLabel("")
        self.buy = QLabel("")
        self.sell = QLabel("")
        self.price_line = QLabel("")
        self.badges = QLabel("")
        self.badges.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
        form.addRow("Owner", self.owner)
        form.addRow("Meta", self.meta)
        form.addRow("Location", self.location)
        form.addRow("", self.place)
        form.addRow("Quantity", self.quantity)
        form.addRow("Volume", self.volume)
        form.addRow("Buy", self.buy)
        form.addRow("Sell", self.sell)
        form.addRow("Price", self.price_line)
        form.addRow("", self.badges)
        root.addLayout(form)

        # The rolls section. Hidden for every ordinary row; show_row shows
        # it in a loading state for a mutated module and the host fills it
        # through show_rolls once its query lands.
        self.rolls_box = QWidget()
        rolls = QVBoxLayout(self.rolls_box)
        rolls.setContentsMargins(0, 8, 0, 0)
        rolls.setSpacing(6)
        self.rolls_header = _caption_label("Rolled stats")
        header_font = self.rolls_header.font()
        header_font.setWeight(QFont.Weight.DemiBold)
        self.rolls_header.setFont(header_font)
        rolls.addWidget(self.rolls_header)
        self.rolls_rows = QVBoxLayout()
        self.rolls_rows.setSpacing(9)
        rolls.addLayout(self.rolls_rows)
        # The source line closes the section under a hairline, the way the
        # strip rules itself off from the table; the rule stays for the
        # status texts too so the block keeps one shape whatever it says.
        self.rolls_note = QLabel("")
        self.rolls_note.setStyleSheet(
            f"color: {palette.SECONDARY_TEXT}; border-top: 1px solid palette(dark);"
            " padding-top: 4px;"
        )
        self.rolls_note.setWordWrap(True)
        rolls.addWidget(self.rolls_note)
        self.fetch_abyssal_btn = QPushButton("Fetch abyssal stats")
        self.fetch_abyssal_btn.clicked.connect(self.fetch_abyssal_clicked)
        rolls.addWidget(self.fetch_abyssal_btn)
        self.roll_rows: list[_RollRow] = []
        self.rolls_box.setVisible(False)
        root.addWidget(self.rolls_box)

        buttons = QVBoxLayout()
        buttons.setSpacing(4)
        self.where_else_btn = QPushButton("Where else?")
        self.where_else_btn.clicked.connect(self.where_else_clicked)
        buttons.addWidget(self.where_else_btn)
        self.refresh_btn = QPushButton("Refresh price")
        self.refresh_btn.clicked.connect(self.refresh_price_clicked)
        buttons.addWidget(self.refresh_btn)
        self.pin_btn = QPushButton("Pin price…")
        self.pin_btn.clicked.connect(self.pin_price_clicked)
        buttons.addWidget(self.pin_btn)
        root.addLayout(buttons)
        root.addStretch(1)

        shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self.close_clicked)

    def show_row(self, row) -> None:
        """Render one ASSET_ROWS row. Called again with a fresh row whenever
        the host reloads, so the panel never shows numbers the table has
        already moved past."""
        self.title.setText(row["item"] or "")
        custom = row["custom_name"] or ""
        self.subtitle.setText(custom)
        self.subtitle.setVisible(bool(custom))
        self.owner.setText(row["owner"] or "")
        meta = row["meta"] or ""
        self.meta.setText(meta)
        self.meta.setVisible(bool(meta))
        wash = palette.rarity_hex(_RARITY_META_IDS.get(meta))
        if wash is None:
            pair = palette.CHIP_ACCENT
            wash = pair[1] if palette.is_dark() else pair[0]
        self.meta.setStyleSheet(f"background: {wash}; border-radius: 8px; padding: 0 6px;")
        flag = row["location_flag"] or ""
        location = row["location"] or ""
        self.location.setText(f"{location} · {flag}" if flag else location)
        self.place.setText(" · ".join(p for p in (row["system"], row["region"]) if p))
        self.quantity.setText(fmt_num(row["quantity"]))
        self.volume.setText(
            f"{row['unit_volume']:,.2f} m³ each · {row['volume']:,.0f} m³ total"
        )
        self.buy.setText(f"{fmt_isk(row['buy_price'])} · lot {fmt_short_isk(row['buy_value'])}")
        self.sell.setText(
            f"{fmt_isk(row['sell_price'])} · lot {fmt_short_isk(row['sell_value'])}"
        )
        source = row["price_source"] or "none"
        self.price_line.setText(_price_text(source, row["price_updated_at"]))
        self.badges.setText(" · ".join(_badges(row, source)))
        # The host decides what the pin dialog does; the caption only has to
        # be honest about which of the two conversations the click starts.
        self.pin_btn.setText("Unpin / change…" if source == "manual" else "Pin price…")
        # A mutated module's rolls arrive from a separate query; until it
        # lands the section says so rather than showing the previous item's
        # rolls under this item's name.
        if _row_get(row, "is_dynamic_type"):
            self._show_rolls_loading()
        else:
            self.show_rolls(None)

    def show_rolls(self, payload: dict | None) -> None:
        """Render one queries.fetch_abyssal_rolls payload, or hide the section.

        None is the ordinary-row case (the panel is unchanged for anything
        that is not a mutated module). A payload always shows the section:
        status 'ok' lists the rolled attributes under a source/mutator line;
        'unfetched' and 'missing' say so and offer the fetch button, labelled
        Retry for an item ESI has already 404'd so the user knows they are
        asking again rather than for the first time.
        """
        self._clear_roll_rows()
        if payload is None:
            self.rolls_box.setVisible(False)
            return
        self.rolls_box.setVisible(True)
        status = payload.get("status") or abyssal.STATUS_UNFETCHED
        mutator = payload.get("mutator")
        for roll in payload.get("rolls") or []:
            row = _RollRow(roll, mutator)
            self.rolls_rows.addWidget(row)
            self.roll_rows.append(row)
        if status == abyssal.STATUS_OK:
            self.rolls_note.setText(source_markup(payload.get("source"), mutator))
        elif status == abyssal.STATUS_MISSING:
            self.rolls_note.setText("ESI has no record of this item")
        else:
            self.rolls_note.setText("Stats not fetched yet")
        self.fetch_abyssal_btn.setVisible(status != abyssal.STATUS_OK)
        self.fetch_abyssal_btn.setEnabled(True)
        self.fetch_abyssal_btn.setText(
            "Retry" if status == abyssal.STATUS_MISSING else "Fetch abyssal stats"
        )

    def set_rolls_fetching(self) -> None:
        """The host started a fetch for this item: hold the button down so a
        second click cannot queue a second request for the same rolls."""
        self.fetch_abyssal_btn.setEnabled(False)
        self.rolls_note.setText("Fetching from ESI…")

    def _show_rolls_loading(self) -> None:
        self._clear_roll_rows()
        self.rolls_box.setVisible(True)
        self.rolls_note.setText("Loading rolled stats…")
        self.fetch_abyssal_btn.setVisible(False)

    def _clear_roll_rows(self) -> None:
        for row in self.roll_rows:
            self.rolls_rows.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self.roll_rows = []


class InspectorWindow(QDialog):
    """The Inspector's top-level home: one reusable, non-modal window.

    The host creates exactly one of these and shows or hides it, rather than
    building a dialog per open the way FitDialog does, so the size and place
    the user drags it to survive from one row to the next. It is a QDialog
    for its close semantics -- Esc and the title-bar close both land in
    reject(), so the host has a single finished() to hook its cleanup on --
    but carries the Qt.Window flag so it gets an ordinary window frame and can
    be minimised or maximised on its own.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.Window)
        self.inspector = Inspector(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.inspector)
        self.resize(420, 640)
        # A QDialog promotes its first autoDefault push button to the default
        # button and clicks it on Enter. That would make Enter in this window
        # fire "Where else?" (or a fetch) out of nowhere; the panel never did
        # anything on Enter, and Space still activates a focused button.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

    def show_row(self, row) -> None:
        """Render the row and name the window after it, so the taskbar and
        the title bar say which item this is without opening it."""
        self.inspector.show_row(row)
        self.setWindowTitle(f"Inspect — {row['item'] or ''}")


class _RollRow(QWidget):
    """One rolled attribute: name and figures, a meter, the range's ends.

    The meter always runs worst to best, left to right, whichever way the
    number itself runs, so a fill reaching right is a good roll on every
    row and the eye compares rows without reading a unit. The label line
    carries the exact value and the signed delta against the source module,
    right-aligned so the figures of neighbouring rows line up; the range
    labels under the meter spell out which value each end is,
    which is what lets a low-is-good CPU figure read correctly with its big
    number on the left.
    """

    def __init__(self, roll: dict, mutator: str | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.roll = roll
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(8)
        # Word-wrapped with the stretch, so a long display name folds under
        # itself instead of pushing the figures off the right edge.
        self.label = QLabel(roll.get("label") or "?")
        self.label.setWordWrap(True)
        head.addWidget(self.label, 1)
        self.value = QLabel(_value_markup(roll))
        self.value.setTextFormat(Qt.RichText)
        self.value.setAlignment(Qt.AlignRight | Qt.AlignTop)
        head.addWidget(self.value, 0, Qt.AlignTop)
        col.addLayout(head)

        # The 4 px and 2 px gaps are measured to the track; the
        # meter widget is taller than its track by the tick's overhang on
        # each side, so the layout gaps shrink by that much.
        col.addSpacing(4 - _RollMeter.TICK_OVERHANG)
        self.meter = _RollMeter(roll)
        col.addWidget(self.meter)
        col.addSpacing(2 - _RollMeter.TICK_OVERHANG)

        ends = QHBoxLayout()
        ends.setSpacing(4)
        texts = _range_texts(roll)
        alignments = (Qt.AlignLeft, Qt.AlignHCenter, Qt.AlignRight)
        labels = []
        for text, alignment in zip(texts, alignments, strict=True):
            label = QLabel(text)
            label.setStyleSheet(f"color: {palette.SECONDARY_TEXT};")
            label.setFont(caption_font(self.label.font()))
            label.setAlignment(alignment | Qt.AlignVCenter)
            ends.addWidget(label, 1)
            labels.append(label)
        self.range_labels: tuple[QLabel, QLabel, QLabel] = tuple(labels)
        col.addLayout(ends)
        self.setToolTip(roll_tooltip(roll, mutator))


class _RollMeter(QWidget):
    """Segmented track from the mutator's worst roll to its best, with the
    source module's base ticked and the base-to-roll span filled.

    fill_from, fill_to and base_pos are kept as 0..1 fractions of the width
    rather than derived from pixels, for the rail bar's reason: the geometry
    is then testable offscreen, where nothing is painted. All three are None
    for an unrankable roll, which paints the bare track. The fill runs from
    the base towards the roll -- so it is the delta made visible, coloured by
    the same verdict as the delta figure -- and is zero-width when the roll
    equals the base. Antialiasing stays off: the 14/1 segment rhythm reads
    as crisp pixels or not at all.
    """

    TRACK_HEIGHT = 6
    TICK_OVERHANG = 2
    TICK_WIDTH = 2

    def __init__(self, roll: dict, parent: QWidget | None = None):
        super().__init__(parent)
        ends = _range_ends(roll)
        self.base_pos = _pos(roll.get("base"), ends)
        value_pos = _pos(roll.get("value"), ends)
        self.better = roll.get("better")
        if self.base_pos is None or value_pos is None:
            self.fill_from = self.fill_to = None
        else:
            self.fill_from, self.fill_to = self.base_pos, value_pos
        self.setFixedHeight(self.TRACK_HEIGHT + 2 * self.TICK_OVERHANG)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        width = self.width()
        paint_segments(
            painter, 0, self.TICK_OVERHANG, width, self.TRACK_HEIGHT,
            palette.track_colour(self.palette()),
        )
        # A verdict of None with a non-zero span cannot come from queries
        # (verdict() is None only for equal or unknown values); a hand-built
        # payload that manages it gets the tick and no fill rather than a
        # fill in a colour that would claim a verdict nobody reached.
        if self.fill_from is not None and self.fill_to != self.fill_from and self.better is not None:
            x0 = round(width * min(self.fill_from, self.fill_to))
            x1 = round(width * max(self.fill_from, self.fill_to))
            if x1 > x0:
                painter.save()
                painter.setClipRect(x0, self.TICK_OVERHANG, x1 - x0, self.TRACK_HEIGHT)
                colour = QColor(palette.delta_hex(bool(self.better), self.palette()))
                paint_segments(painter, 0, self.TICK_OVERHANG, width, self.TRACK_HEIGHT, colour)
                painter.restore()
        if self.base_pos is not None:
            x = round(width * self.base_pos) - self.TICK_WIDTH // 2
            x = min(max(x, 0), max(width - self.TICK_WIDTH, 0))
            painter.fillRect(
                x, 0, self.TICK_WIDTH, self.height(), self.palette().color(QPalette.WindowText)
            )


def _display_high_is_good(roll: dict) -> bool:
    """Whether a bigger DISPLAYED number is the better roll, read off the
    verdict when there is one: ``better`` with the sign of the displayed
    delta fixes the direction and makes the fill's colour and its end agree
    by construction. Equal or unknown values fall back to
    abyssal.display_high_is_good."""
    better, value, base = roll.get("better"), roll.get("value"), roll.get("base")
    if better is not None and value is not None and base is not None and value != base:
        return bool(better) == (float(value) > float(base))
    return abyssal.display_high_is_good(roll.get("high_is_good"), roll.get("unit_id"))


def _range_ends(roll: dict) -> tuple[float, float] | None:
    """(worst, best) in display values, or None when the roll is unrankable.

    An equal pair is unrankable too: queries never sends one (roll_position
    is None for a degenerate range) but a hand-built payload might, and a
    zero-width range has no position to divide by.
    """
    lo, hi = roll.get("min"), roll.get("max")
    if lo is None or hi is None or lo == hi:
        return None
    lo, hi = float(lo), float(hi)
    return (lo, hi) if _display_high_is_good(roll) else (hi, lo)


def _pos(value, ends: tuple[float, float] | None) -> float | None:
    """Where a display value sits on the worst-to-best axis, clamped to 0..1
    so a roll a hair outside the range we hold (float noise, a source whose
    real base differs from ours) still draws at the end rather than off it."""
    if value is None or ends is None:
        return None
    worst, best = ends
    return min(1.0, max(0.0, (float(value) - worst) / (best - worst)))


def _range_texts(roll: dict) -> tuple[str, str, str]:
    """Left, centre and right labels under the meter."""
    ends = _range_ends(roll)
    if ends is None:
        return ("", "range unknown", "")
    worst, best = ends
    return (_fmt(roll, worst), f"base {_fmt(roll, roll.get('base'))}", _fmt(roll, best))


def _value_markup(roll: dict) -> str:
    """Rich text for the label line's right column: the rolled value with
    its unit, then the delta against the base signed with a true minus and
    coloured by the verdict.

    The delta carries no unit -- the value beside it already says which --
    and no unit id either, because format_value forces its own "+" for the
    signed modifier units and the sign here is chosen from the delta. A
    delta that rounds to nothing reads "±0.00" in the muted colour whatever
    the verdict says about the unrounded numbers, so the colour never
    contradicts the figure the user can see.
    """
    value, base, better = roll.get("value"), roll.get("base"), roll.get("better")
    text = html.escape(_fmt(roll, value))
    if value is None or base is None:
        return text
    delta = float(value) - float(base)
    magnitude = abyssal.format_value(abs(delta), None, None)
    if float(magnitude.replace(",", "")) == 0 or better is None:
        sign, colour = "±", palette.SECONDARY_TEXT
    else:
        sign = "+" if delta > 0 else "−"
        colour = palette.delta_hex(bool(better))
    return f"{text} <span style='color: {colour};'>{sign}{html.escape(magnitude)}</span>"


def source_markup(source: str | None, mutator: str | None) -> str:
    """The section's closing line: which module was mutated, and by which
    tier of mutaplasmid, the tier in the WARN colour because it is the one
    word a buyer scans for (a Radical roll is a different market from a
    Decayed one). The tier is the mutator name's first word -- CCP names
    every mutaplasmid "<Tier> <Module> Mutaplasmid", checked against the SDE
    type list on 2026-09-02."""
    text = html.escape(source or "unknown source")
    if not mutator:
        return f"{text} · mutaplasmid unknown"
    tier = html.escape(mutator.split()[0])
    warn = palette.status_hex(palette.WARN)
    return f"{text} · <span style='color: {warn};'>{tier}</span> mutaplasmid"


def _fmt(roll: dict, value) -> str:
    """Display text for one of the roll's numbers, unit symbol attached.

    unit_id is read with .get(): the pinned payload carries the unit symbol
    but not the id, and format_value only needs the id to pick a sign
    convention for modifier units -- without it the number still renders.
    """
    if value is None:
        return "—"
    return abyssal.format_value(float(value), roll.get("unit_id"), roll.get("unit"))


def roll_text(roll: dict) -> str:
    """`label: value · NN% of range · ▲ +Δ vs base`, degrading a piece at a
    time when the payload lacks the range or the base."""
    parts = [f"{roll.get('label') or '?'}: {_fmt(roll, roll.get('value'))}"]
    quality = roll.get("quality")
    parts.append("range unknown" if quality is None else f"{round(quality * 100)}% of range")
    value, base = roll.get("value"), roll.get("base")
    if value is not None and base is not None:
        delta = float(value) - float(base)
        better = roll.get("better")
        glyph = "=" if better is None or delta == 0 else ("▲" if better else "▼")
        sign = "+" if delta > 0 else ("-" if delta < 0 else "")
        # The magnitude is formatted without the unit id on purpose: for the
        # signed modifier units format_value forces its own "+", which would
        # collide with the sign chosen here ("-+1.20%").
        magnitude = abyssal.format_value(abs(delta), None, roll.get("unit"))
        parts.append(f"{glyph} {sign}{magnitude} vs {_fmt(roll, base)}")
    return " · ".join(parts)


def roll_tooltip(roll: dict, mutator: str | None) -> str:
    """Position and quality in words, then the source module and mutator.

    The range's ends are not repeated here since the labels under the meter
    show them. The position is given in DISPLAY terms -- how far along the
    labelled low-to-high axis the roll sits -- which is the quality itself
    when a bigger displayed number is better and its mirror otherwise; for
    a low-is-good attribute the two figures differ and this is where the
    difference gets explained, because a user comparing them would
    otherwise think one was wrong.
    """
    lines = []
    quality = roll.get("quality")
    if quality is not None:
        high = _display_high_is_good(roll)
        shown = quality if high else 1.0 - quality
        line = f"Rolled {round(shown * 100)}% of the way from the range's low end to its high end"
        if not high:
            line += f"; lower is better here, so the roll quality is {round(quality * 100)}%"
        lines.append(line)
    if roll.get("base") is not None:
        lines.append(f"Source module: {_fmt(roll, roll['base'])}")
    lines.append(f"Mutator: {mutator or 'unknown'}")
    return "\n".join(lines)


def _price_text(source: str, updated_at) -> str:
    """Source plus quote age, or the plain truth when there is no quote."""
    if source == "none":
        return "unpriced"
    updated = _parse_utc(updated_at)
    if updated is None:
        return source
    hours = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
    when = f"{max(int(hours), 0)} h ago" if hours < 48 else f"{int(hours // 24)} d ago"
    return f"{source} · {when}"


def _badges(row, source: str) -> list[str]:
    parts: list[str] = []
    if source == "none":
        parts.append("unpriced")
    elif source == "manual":
        parts.append("manual price")
    if row["is_blueprint_copy"]:
        parts.append("BPC")
    return parts
