"""Point every test at a scratch data directory before evasset is imported.

config.DATA_DIR is resolved at import time, so this has to happen in conftest
rather than in any individual test module -- otherwise the first test file
that happens to be collected decides where the whole suite writes, and a run
of a single Qt test would go straight at the real database.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="evasset-tests-")
os.environ.setdefault("EVEBOOTY_DATA_DIR", _TMP)
os.environ.setdefault("EVEBOOTY_CACHE_DIR", _TMP)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from evasset.esi.client import ESIError  # noqa: E402

# The one real abyssal item the suite is built around: a Ballistic Control
# System whose ids and ESI body come from docs/research/abyssal-stats.md
# section 1.4, verbatim. Shared here because the write path, the assets
# view and the main window each store or fetch it and must agree on it.
BCS_TYPE, BCS_SOURCE, BCS_MUTATOR = 49738, 13935, 49740
BCS_BODY = {
    "created_by": 90000001,
    "dogma_attributes": [
        {"attribute_id": 4, "value": 1.0}, {"attribute_id": 9, "value": 40.0},
        {"attribute_id": 204, "value": 0.8828844567859173},
        {"attribute_id": 213, "value": 1.1077080251407625},
        {"attribute_id": 1692, "value": 4.0}, {"attribute_id": 30, "value": 1.0},
        {"attribute_id": 161, "value": 5.0}, {"attribute_id": 162, "value": 1.0},
        {"attribute_id": 422, "value": 1.0}, {"attribute_id": 38, "value": 0.0},
        {"attribute_id": 50, "value": 25.799999713897705},
        {"attribute_id": 182, "value": 3318.0}, {"attribute_id": 633, "value": 8.0},
        {"attribute_id": 277, "value": 1.0},
    ],
    "dogma_effects": [{"effect_id": 11, "is_default": False}],
    "mutator_type_id": BCS_MUTATOR,
    "source_type_id": BCS_SOURCE,
}


class FakeESIClient:
    """Stands in for ESIClient: serves canned dynamic-item bodies, records
    every path asked for and the keyword arguments it was asked with, and
    never opens a socket."""

    def __init__(self, bodies=None, errors=()):
        self.bodies = dict(bodies or {})
        self.errors = set(errors)
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.closed = False

    def get(self, path, **kw):
        self.calls.append(path)
        self.kwargs.append(kw)
        item_id = int(path.rsplit("/", 1)[-1])
        if item_id in self.errors:
            raise ESIError(502, path, "bad gateway")
        return self.bodies.get(item_id)  # None is what allow_404 yields on a 404

    @property
    def requested_items(self) -> list[int]:
        return [int(p.rsplit("/", 1)[-1]) for p in self.calls]

    def close(self) -> None:
        self.closed = True


def match_text(card) -> str:
    """The search card's footer as one string: count and the rest together."""
    return f"{card.match_count_label.text()} {card.match_rest_label.text()}".strip()


@pytest.fixture()
def qapp_or_skip():
    """One QApplication for any test that needs widgets, skipped without Qt."""
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def started_tasks(monkeypatch):
    """TaskManager._start replaced by a recorder, so a window's jobs are
    listed instead of run and nothing reaches the network. The real _start
    flips the task to RUNNING; without that the pump would start the same
    task again on the next submit."""
    from evasset.ui.tasks import RUNNING, TaskManager

    started = []

    def fake_start(self, task):
        task.state = RUNNING
        started.append(task)

    monkeypatch.setattr(TaskManager, "_start", fake_start)
    return started
