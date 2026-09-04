"""Status bar task centre.

Replaces the single label + single progress bar that could only ever describe
one job. Collapsed it is one line; click it and every running task is listed
with its own progress and its own cancel button.

Nothing here starts or stops work directly -- it renders a TaskManager and
emits cancel_requested. Keeping the view free of scheduling logic is what
lets the same manager be rendered somewhere else (the characters dialog does
exactly that) without two views disagreeing about what is running.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .palette import SECONDARY_TEXT
from .tasks import RUNNING, TaskManager


class _TaskRow(QWidget):
    """One task in the expanded list."""

    cancel_requested = Signal(int)

    def __init__(self, task, parent: QWidget | None = None):
        super().__init__(parent)
        self.task_id = task.id

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = QLabel(task.label)
        self.title.setStyleSheet("font-weight: 600;")
        top.addWidget(self.title, 1)

        self.cancel = QPushButton("Cancel")
        self.cancel.setFixedHeight(24)
        self.cancel.setToolTip(f"Stop {task.label}")
        # Icon-only would be tidier but leaves screen readers with nothing to
        # announce, and a bare glyph is not obviously a button.
        self.cancel.setAccessibleName(f"Cancel {task.label}")
        self.cancel.clicked.connect(lambda: self.cancel_requested.emit(self.task_id))
        top.addWidget(self.cancel, 0)
        outer.addLayout(top)

        self.bar = QProgressBar()
        self.bar.setFixedHeight(6)
        self.bar.setTextVisible(False)
        outer.addWidget(self.bar)

        self.detail = QLabel("")
        self.detail.setStyleSheet(f"color: {SECONDARY_TEXT};")
        outer.addWidget(self.detail)

        self.update_from(task)

    def update_from(self, task) -> None:
        self.detail.setText(task.message or "Waiting to start…")
        if task.state == RUNNING:
            self.bar.setRange(0, 100)
            self.bar.setValue(task.percent)
        else:                       # queued behind something else
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
        self.cancel.setEnabled(not task.cancelling)
        if task.cancelling:
            self.cancel.setText("Cancelling…")


class _TaskPopup(QFrame):
    cancel_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.Popup)
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(340)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._rows: dict[int, _TaskRow] = {}

    def render(self, tasks) -> None:
        seen = set()
        for task in tasks:
            seen.add(task.id)
            row = self._rows.get(task.id)
            if row is None:
                row = _TaskRow(task)
                row.cancel_requested.connect(self.cancel_requested)
                self._rows[task.id] = row
                self._layout.addWidget(row)
            row.update_from(task)
        for task_id in list(self._rows):
            if task_id not in seen:
                row = self._rows.pop(task_id)
                self._layout.removeWidget(row)
                row.deleteLater()
        self.adjustSize()


class TaskBar(QWidget):
    """Collapsed summary for the status bar, expanding to the full list."""

    cancel_requested = Signal(int)

    def __init__(self, tasks: TaskManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.tasks = tasks
        self._idle_text = ""

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.summary = QToolButton()
        self.summary.setAutoRaise(True)
        self.summary.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.summary.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        # A stylesheet on a QToolButton takes over its painting, focus ring
        # included, so the ring has to be put back explicitly or the control
        # becomes invisible to keyboard users.
        self.summary.setStyleSheet(
            "QToolButton { text-align: left; padding: 2px 4px; border: 1px solid transparent; }"
            "QToolButton:focus { border: 1px solid palette(highlight); }"
        )
        self.summary.setFocusPolicy(Qt.StrongFocus)
        self.summary.clicked.connect(self._toggle_popup)
        layout.addWidget(self.summary, 1)

        self.bar = QProgressBar()
        self.bar.setMaximumWidth(160)
        self.bar.setFixedHeight(12)
        self.bar.setTextVisible(False)
        self.bar.setVisible(False)
        layout.addWidget(self.bar, 0)

        self.popup = _TaskPopup(self)
        self.popup.cancel_requested.connect(self.cancel_requested)

        tasks.changed.connect(self.refresh)
        self.refresh()

    def set_idle_text(self, text: str) -> None:
        self._idle_text = text
        self.refresh()

    # ------------------------------------------------------------- rendering
    def refresh(self) -> None:
        active = self.tasks.active()
        if not active:
            self.summary.setText(self._idle_text)
            self.summary.setEnabled(False)
            self.summary.setToolTip("")
            self.summary.setAccessibleName(self._idle_text)
            self.bar.setVisible(False)
            self.popup.hide()
            return

        self.summary.setEnabled(True)
        self.bar.setVisible(True)
        self.bar.setValue(self.tasks.overall_percent())

        # No spinner glyph. U+27F3 and friends are not in every Windows UI
        # font and fall back to a replacement box, and a symbol carrying the
        # "something is happening" message on its own gives a screen reader
        # nothing to read. The progress bar beside this says it visually; the
        # accessible name says it in words.
        if len(active) == 1:
            task = active[0]
            detail = task.message or "Starting…"
            self.summary.setText(detail)
            self.summary.setToolTip(f"{task.label} - click for detail")
            self.summary.setAccessibleName(f"Running: {task.label}. {detail}")
        else:
            running = sum(1 for t in active if t.state == RUNNING)
            queued = len(active) - running
            text = f"{running} task{'s' if running != 1 else ''} running"
            if queued:
                text += f", {queued} queued"
            self.summary.setText(text + "  (click for detail)")
            self.summary.setToolTip("Click to see each task")
            self.summary.setAccessibleName(text)

        if self.popup.isVisible():
            self.popup.render(active)
            self._place_popup()

    # --------------------------------------------------------------- popup
    def _toggle_popup(self) -> None:
        if self.popup.isVisible():
            self.popup.hide()
            return
        active = self.tasks.active()
        if not active:
            return
        self.popup.render(active)
        self._place_popup()
        self.popup.show()

    def _place_popup(self) -> None:
        corner = self.mapToGlobal(self.rect().topLeft())
        self.popup.move(corner.x(), corner.y() - self.popup.height() - 4)
