#!/usr/bin/env python3
"""Build a standalone executable with Nuitka.

    uv run python build.py            # standalone folder in dist/
    uv run python build.py --onefile  # single exe, slower to start

Nuitka's PySide6 plugin pulls in the Qt plugins and the modules actually
imported, so QtCharts comes along without listing it by hand. The SDE is not
bundled -- the app downloads it on first run and checks CCP's build number for
updates after that, which is the whole point of not shipping a 95 MB blob that
goes stale every patch day.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ENTRY = ROOT / "src" / "evasset" / "__main__.py"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--onefile", action="store_true", help="produce a single exe")
    parser.add_argument("--console", action="store_true", help="keep the console window")
    parser.add_argument("--output", default="dist", help="output directory")
    args = parser.parse_args()

    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        "--enable-plugin=pyside6",
        "--include-package=evasset",
        # keyring finds its OS backend by entry point, which Nuitka cannot see
        "--include-package=keyring.backends",
        "--include-package=jaraco",
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=tkinter",
        f"--output-dir={args.output}",
        "--output-filename=evasset",
        "--company-name=evasset",
        "--product-name=EVE Assets",
        "--product-version=0.1.0",
        "--file-description=EVE Online asset manager",
    ]
    if args.onefile:
        cmd.append("--onefile")
    if sys.platform == "win32" and not args.console:
        cmd.append("--windows-console-mode=disable")
    icon = ROOT / "assets" / "evasset.ico"
    if icon.exists() and sys.platform == "win32":
        cmd.append(f"--windows-icon-from-ico={icon}")
    cmd.append(str(ENTRY))

    print(" ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
