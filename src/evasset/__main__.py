"""Entry point. `uv run evebooty` in dev, or the Nuitka-built exe."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(prog="evebooty", description="EVE Booty - EVE Online asset manager")
    parser.add_argument(
        "--sync", action="store_true", help="sync, reprice and snapshot from the CLI, then exit"
    )
    parser.add_argument(
        "--update-sde", action="store_true", help="download and import the latest SDE, then exit"
    )
    args = parser.parse_args()

    if args.sync or args.update_sde:
        return _headless(args)

    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .config import Settings
    from .ui import MainWindow
    from .ui.workers import StartupInitJob

    app = QApplication(sys.argv)
    app.setApplicationName("EVE Booty")
    app.setOrganizationName("evasset")
    icon = app_icon()
    if icon is not None:
        # Set on the application, not just the window: this is what the
        # taskbar, Alt-Tab and every dialog inherit.
        app.setWindowIcon(icon)

    # Windows 11's dark palette ships Shadow -- the role all muted text in
    # this app draws in -- as pure black, and AlternateBase as pure white.
    # See ui/palette.normalised for the measurements; applied here, once,
    # before any widget reads a colour.
    from .ui.palette import normalised

    app.setPalette(normalised(app.palette()))

    # db.init() -- the schema script, the migration check, and (just once,
    # the first launch after an upgrade that adds one) building a brand new
    # index over however many rows are already in the table -- used to run
    # directly here, before the window was shown and before app.exec() had
    # started pumping the event loop. That is not "slow", it is the process
    # genuinely not processing window messages yet, which is what makes an OS
    # report it as not responding rather than just idle. A visible splash
    # plus running it in the background fixes both the appearance and the
    # actual cause: the window shows immediately, and MainWindow's own
    # db.init() call (on the main thread, once this has already primed the
    # schema on disk) finds nothing left to do.
    splash = _build_splash()
    splash.show()

    state: dict = {}

    def start_main_window(_result=None) -> None:
        state["window"] = MainWindow(Settings.load())
        state["window"].show()
        splash.close()

    def startup_failed(message: str) -> None:
        splash.close()
        QMessageBox.critical(None, "Could not start", f"Failed to open the database:\n{message}")
        app.quit()

    job = StartupInitJob()
    job.signals.finished.connect(start_main_window)
    job.signals.failed.connect(startup_failed)
    QThreadPool.globalInstance().start(job)

    return app.exec()



def app_icon():
    """The chest, loaded from beside the package.

    Returns None rather than raising if it is missing: a build that forgot
    --include-package-data should start with a blank icon, not refuse to run.
    """
    from PySide6.QtGui import QIcon

    # The .ico first: it carries all six sizes, so Qt picks the one drawn for
    # the context (16 px title bar, 32 px taskbar) instead of downscaling the
    # 256 px art and muddying the detail the small sizes deliberately omit.
    assets = Path(__file__).resolve().parent / "assets"
    for name in ("booty.ico", "booty.png"):
        path = assets / name
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return None

def _build_splash():
    from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

    w = QWidget()
    w.setWindowTitle("EVE Booty")
    w.setFixedSize(320, 96)
    layout = QVBoxLayout(w)
    layout.addWidget(QLabel("Opening your database…"))
    bar = QProgressBar()
    bar.setRange(0, 0)  # indeterminate -- a spinner, not a percentage we do not have
    layout.addWidget(bar)
    return w


def _headless(args) -> int:
    """Enough of the app to drive it from a scheduled task."""
    from . import db, networth, pricing, sde
    from .config import Settings
    from .esi import ESIClient, TokenCache
    from .esi.sync import Syncer

    settings = Settings.load()
    conn = db.init()

    def show(msg: str, pct: int) -> None:
        print(f"[{pct:3d}%] {msg}", flush=True)

    if args.update_sde:
        updated, build = sde.ensure_current(conn, settings, show)
        print(f"SDE build {build} {'imported' if updated else 'already current'}")
        if not args.sync:
            return 0

    tokens = TokenCache(settings)
    client = ESIClient(settings, tokens)
    try:
        syncer = Syncer(conn, client, settings)
        chars = syncer.enabled_characters()
        if not chars:
            print("No enabled characters. Add one in the GUI first.", file=sys.stderr)
            return 1
        for row in chars:
            for warning in syncer.sync_character(row, show):
                print(f"  warning: {warning}", file=sys.stderr)
        stats = pricing.refresh_prices(conn, client, settings, show)
        print(f"Priced {stats.get('total', 0)} types")
        networth.take_snapshot(conn)
        networth.prune_snapshots(conn)
    finally:
        client.close()

    for b in networth.compute_all(conn):
        print(f"{b.owner_name:<28} {b.total:>22,.2f} ISK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
