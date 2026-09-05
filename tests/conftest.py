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


@pytest.fixture()
def qapp_or_skip():
    """One QApplication for any test that needs widgets, skipped without Qt."""
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
