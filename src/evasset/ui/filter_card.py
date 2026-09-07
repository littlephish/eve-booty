"""The popover shell every omnibox filter card is built on.

Three chips now carry a ``▾`` glyph -- ``abyssal``, ``holds`` and ``fit`` --
and each opens a card that builds its own chips. The abyssal card came first
and settled the parts that are not about any one kind: a Qt.Popup anchored
under the chip rather than a dialog (a modal window over the table would hide
the very rows the filter is narrowing), a fixed width, a title, a body, and a
footer carrying the live "N of TOTAL match" beside Cancel and Done. This is
that shell, lifted out unchanged so the second and third cards inherit the
behaviour rather than re-deriving it -- the abyssal card's own tests are the
regression net for the extraction and run against the subclass untouched.

Popup semantics decide the commit model, and the commit model is the shell's
real content. Qt closes a popup on any outside click, and that close has to
mean something: it means Cancel. ``apply()`` is the only path that emits
``done``, so an exploratory edit abandoned by clicking back on the table
leaves the filter exactly as it was, whichever card was open.

A card is database-free. It renders what the owning view feeds it, announces
``filter_changed`` whenever the chips Done would write have changed, and
hears back a count for the footer. Two shapes of count exist, which is why
``totals()`` is part of the subclass contract rather than a rule here: the
abyssal card counts items under its own chips, while the holds and fit cards
count assembled ships, and only the card knows which question its footer is
asking. A third shape asks nothing: the save and load cards act on the view
library rather than on the filter, so they set ``SHOWS_COUNT = False`` and
the footer carries Cancel and Done alone.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import palette
from .strip import caption_font

CARD_WIDTH = 440


class FilterCard(QFrame):
    """The shared popover: title, a body slot, and the count/Cancel/Done footer.

    Subclasses fill ``head_layout`` (under the title) and ``body_layout``, and
    implement ``seed``, ``chips``, ``totals`` and -- where the default is
    wrong -- ``owns``. ``KINDS`` is what Done replaces: the view keeps every
    chip the card does not own and appends what ``chips()`` returns, so a
    typed chip of a kind this card cannot express rides through Done untouched.
    """

    # The chip kinds Done replaces, and the card's heading.
    KINDS: tuple[str, ...] = ()
    TITLE = ""
    WIDTH = CARD_WIDTH
    # Whether the footer asks "N of TOTAL match". False for a card that
    # writes no chips at all -- the save and load command cards act on the
    # library rather than on the filter, so there is no denominator and no
    # question worth asking. The two labels go; the sentence layout stays,
    # so the footer keeps its height and Cancel and Done keep their place.
    SHOWS_COUNT: bool = True

    filter_changed = Signal()   # the chips Done would write have changed
    done = Signal(list)         # omni.Chip list to replace the card's kinds
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.Popup)
        self.setFixedWidth(self.WIDTH)
        self.setFrameShape(QFrame.StyledPanel)
        self.setObjectName("filtercard")
        self.setStyleSheet(
            "#filtercard { border: 1px solid palette(shadow); border-radius: 6px;"
            " background: palette(window); }"
        )
        self._applied = False
        small = caption_font(self.font())

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        head = QWidget()
        self.head_layout = QVBoxLayout(head)
        self.head_layout.setContentsMargins(12, 10, 12, 8)
        self.head_layout.setSpacing(6)
        self.title_label = QLabel(self.TITLE)
        font = QFont(self.title_label.font())
        font.setWeight(QFont.Weight.DemiBold)
        font.setPointSizeF(font.pointSizeF() + 0.75)
        self.title_label.setFont(font)
        self.head_layout.addWidget(self.title_label)
        root.addWidget(head)

        body = QWidget()
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(12, 6, 12, 10)
        self.body_layout.setSpacing(12)
        root.addWidget(body)

        # The footer sits a step above the window with a rule over it, both
        # derived from the palette (toward_text) so they exist on every
        # theme; the bottom corners follow the card's radius so the surface
        # does not poke square corners out of the rounded border.
        pal = self.palette()
        self.footer = QFrame()
        self.footer.setObjectName("filtercardfooter")
        self.footer.setStyleSheet(
            "#filtercardfooter {"
            f" background: {palette.toward_text(pal, 0.05).name()};"
            f" border-top: 1px solid {palette.track_colour(pal).name()};"
            " border-bottom-left-radius: 5px; border-bottom-right-radius: 5px; }"
        )
        foot = QHBoxLayout(self.footer)
        foot.setContentsMargins(12, 8, 12, 8)
        foot.setSpacing(8)
        # Kept on the instance so a card that shows no count can put its own
        # status where the count sentence would sit -- the save card's
        # "N saved views" -- rather than reaching into a local.
        self.footer_layout = foot
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
        if not self.SHOWS_COUNT:
            self.match_count_label.setVisible(False)
            self.match_rest_label.setVisible(False)
            # A nested layout whose children are all hidden counts as empty
            # and forfeits its stretch, which handed the buttons the whole
            # footer width; the stretch keeps them at the right, as on a
            # card with a count.
            foot.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.hide)
        foot.addWidget(self.cancel_btn)
        self.done_btn = QPushButton("Done")
        self.done_btn.setToolTip("Apply (Enter)")
        self.done_btn.clicked.connect(self.apply)
        foot.addWidget(self.done_btn)
        root.addWidget(self.footer)

    # ------------------------------------------------------- subclass contract
    def seed(self, chips: list) -> None:
        """Start over from the omnibox's current chips.

        Nothing is announced here: the view fetches this card's data straight
        after, and that fetch announces the filter once the widgets it fills
        exist.
        """

    def chips(self) -> list:
        """The omni.Chip list Done hands back, replacing the card's KINDS."""
        return []

    def owns(self, chip) -> bool:
        """Whether Done replaces this chip.

        Positive chips of the card's own kinds by default: a negated one is
        usually beyond what a card can express, so it must ride through Done
        rather than be swallowed.
        """
        return chip.kind in self.KINDS and not chip.negated

    def totals(self) -> tuple[str, object]:
        """What the footer's denominator counts, as a (shape, argument) pair.

        ``("chips", [Chip, ...])`` counts assets under the card's own chips,
        ``("ships", hull_type_id | None)`` counts assembled ships, and
        ``("none", None)`` asks nothing -- the view runs no count query at
        all, which is what a card with ``SHOWS_COUNT = False`` wants. The
        view dispatches on the shape; the card knows which question it is
        asking.
        """
        return "chips", []

    def first_focus(self) -> QWidget | None:
        """The widget the card opens focused on, or None to leave focus be."""
        return None

    # ------------------------------------------------------------------- API
    def set_match_count(self, matched: int, total: int) -> None:
        """The footer's live answer: matched rows out of the card's own denominator.

        The count wears the primary text colour and the rest the muted one,
        so the figure is what the eye lands on. Inert on a card that shows
        no count, so a stray answer from a count already in flight cannot
        make the labels reappear.
        """
        if not self.SHOWS_COUNT:
            return
        self.match_count_label.setText(f"{matched:,}")
        self.match_rest_label.setText(f"of {total:,} match")

    def apply(self) -> None:
        """Done: hand the chips over once the popup is hidden.

        Hiding first makes the hide read as applied, not cancelled, and by
        the time the chips go out the popup is gone, so the view's set_spec
        reload paints under nothing.
        """
        chips = self.chips()
        self._applied = True
        self.hide()
        self.done.emit(chips)

    # ------------------------------------------------------------- internals
    def _announce(self) -> None:
        """Tell the view the chips Done would write have changed.

        The count shows as pending until the view answers.
        """
        self._set_counting()
        self.filter_changed.emit()

    def _set_counting(self) -> None:
        if not self.SHOWS_COUNT:
            return
        self.match_count_label.setText("…")

    # ---------------------------------------------------------------- events
    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.hide()
            event.accept()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            # Enter anywhere in the card but an edit (a bound field's row
            # commits it, a search edit selects on it, a paste box takes the
            # newline) is Done: the buttons ignore the key, so it arrives
            # here. A Done the card has disabled does nothing at all, rather
            # than applying an empty chip list -- on the holds and fit cards
            # that is the state with no consumable named and no fit picked,
            # and applying it would silently delete the chip the card was
            # opened on.
            if self.done_btn.isEnabled():
                self.apply()
            event.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        self._applied = False
        super().showEvent(event)
        # Focus inside a Qt.Popup is ordinary -- the popup owns the keyboard
        # while it is up -- and showing it hands the keyboard to its focus
        # widget, which this makes the card's own entry point.
        widget = self.first_focus()
        if widget is not None:
            widget.setFocus()

    def hideEvent(self, event) -> None:  # noqa: N802
        """Every way out that is not Done ends here and reads as Cancel.

        Cancel, Esc and an outside click closing the popup all arrive as a
        hide.
        """
        super().hideEvent(event)
        if not self._applied:
            self.cancelled.emit()
