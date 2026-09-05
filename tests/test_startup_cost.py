"""Keeping the pre-splash import path cheap.

The splash never appeared to animate. It was not frozen: it barely existed.
main() imported evasset.ui.workers to get StartupInitJob, Python initialises a
parent package before any submodule, and evasset/ui/__init__ imported
MainWindow, which pulls every view. workers itself imports pricing -> esi.client
-> auth -> python-jose and keyring. That was roughly 590ms with no window on
screen at all, before QApplication existed, after which the splash showed for a
few frames and closed.

The fix has two halves that only work together, so both are pinned here:
StartupInitJob lives in a module that imports nothing expensive, and
evasset.ui defers MainWindow behind a module __getattr__.
"""

from __future__ import annotations

import subprocess
import sys

# Anything on this list drags in python-jose, keyring or httpx, or the entire
# widget set. None of it can be reached before the splash is up.
FORBIDDEN = (
    "evasset.pricing",
    "evasset.esi.client",
    "evasset.esi.auth",
    "evasset.ui.main_window",
    "evasset.ui.assets_view",
    "evasset.ui.workers",
    "jose",
    "keyring",
)

PROBE = """
import sys
import evasset.ui.startup  # noqa: F401
print(",".join(sorted(m for m in sys.modules if m in {names})))
"""


def _modules_after_startup_import() -> set[str]:
    """Import the pre-splash path in a clean interpreter and report which of
    the expensive modules came with it."""
    code = PROBE.format(names=set(FORBIDDEN))
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    return {m for m in out.stdout.strip().split(",") if m}


def test_the_pre_splash_path_stays_cheap():
    """Importing StartupInitJob must not pull the ESI stack or the widgets.

    A regression here is invisible: the app still works, it just goes back to
    showing nothing for half a second and then flashing a splash.
    """
    loaded = _modules_after_startup_import()
    assert not loaded, (
        "evasset.ui.startup pulled in expensive modules: "
        + ", ".join(sorted(loaded))
    )


def test_the_ui_package_does_not_eagerly_import_main_window():
    """The other half. Importing any submodule of evasset.ui initialises the
    package, so an eager re-export here undoes the split above."""
    code = (
        "import sys, evasset.ui;"
        "print('evasset.ui.main_window' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_main_window_is_still_reachable_from_the_package():
    """Lazy, not gone. main() and the tests both do `from evasset.ui import
    MainWindow` and must keep working."""
    from evasset.ui import MainWindow

    assert MainWindow.__name__ == "MainWindow"
