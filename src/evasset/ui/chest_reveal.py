"""A transparent animation played over the window.

Used by the assets view when there is something to show rather than a dialog
to open.

QMovie over an animated WebP, not QtMultimedia over the source .webm.
QMovie.supportedFormats() does not include webm, Media Foundation will not
composite an alpha channel over a widget, and adding QtMultimedia to the build
would cost megabytes. See scripts/make_chest_animation.py.

Frameless, translucent, does not take focus, and dismissed by clicking it or
by leaving it. Nothing that appears unbidden should need working out how to
close.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QMovie
from PySide6.QtWidgets import QLabel, QWidget

ASSET = Path(__file__).resolve().parent.parent / "assets" / "eve-booty-chest.webp"

# Long enough to read as a reward, short enough not to be a hostage situation.
# The animation is a seamless 8.3 second loop, so it is cut off rather than
# played out; there is no ending to miss.
DURATION_MS = 4200


def available() -> bool:
    """Whether the animation can play at all.

    A build that somehow shipped without the asset should quietly do nothing,
    rather than raise from a context menu.
    """
    return ASSET.exists()


class ChestReveal(QWidget):
    """A transparent, frameless window that plays the chest once and leaves."""

    def __init__(self, parent: QWidget | None = None):
        # Qt.Tool rather than Qt.Window: it should not get a taskbar button of
        # its own, and it should not outlive the window that raised it.
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        # Without this the window takes focus. Nothing that appears on its own
        # should interrupt what somebody was typing.
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self._label = QLabel(self)
        self._movie = QMovie(str(ASSET))
        self._label.setMovie(self._movie)

        self._movie.jumpToFrame(0)
        size = self._movie.currentImage().size()
        if size.isEmpty():
            size = self._movie.frameRect().size()
        self.resize(size)
        self._label.resize(size)

        self._movie.start()
        QTimer.singleShot(DURATION_MS, self.close)

    def center_on(self, other: QWidget | None) -> None:
        """Sit in the middle of the window that raised it.

        Falls back to the screen when there is no sensible parent, so this
        cannot end up drawn at (0, 0) in a corner.
        """
        if other is not None and other.isVisible():
            geo = other.geometry()
            self.move(geo.center().x() - self.width() // 2,
                      geo.center().y() - self.height() // 2)
            return
        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2,
                      geo.center().y() - self.height() // 2)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Stop the movie explicitly. QMovie keeps a timer running against a
        # label whose C++ half is about to go away, which is the same shape of
        # teardown race that AsyncQuery had to be guarded against.
        self._movie.stop()
        super().closeEvent(event)


def play(parent: QWidget | None = None) -> ChestReveal | None:
    """Show the chest over `parent`. Returns None when the asset is missing."""
    if not available():
        return None
    reveal = ChestReveal(parent)
    reveal.center_on(parent.window() if parent is not None else None)
    reveal.show()
    return reveal
