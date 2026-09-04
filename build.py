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
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ENTRY = ROOT / "src" / "evasset" / "__main__.py"


def version() -> str:
    """Whatever src/evasset/_version.py says, read without importing the
    package (which would drag in PySide6 just to learn a string).

    A local build therefore stamps the .dev version a checkout carries, not a
    release number -- releases are built by CI, which rewrites that file from
    the tag first. Nothing here should ever hardcode a version: this used to
    say 0.1.0 and stayed saying it.
    """
    text = (ROOT / "src" / "evasset" / "_version.py").read_text(encoding="utf-8")
    match = re.search(r"""__version__\s*=\s*["'](?P<v>[^"']+)["']""", text)
    if match is None:
        raise SystemExit("could not read __version__ from src/evasset/_version.py")
    return match.group("v")


def numeric(version_text: str) -> str:
    """Windows version resources are four integers and reject "0.0.0.dev0",
    so the numeric core is padded out and any suffix dropped -- the same rule
    scripts/set_version.py applies for CI builds."""
    core = re.match(r"\d+(?:\.\d+){0,3}", version_text)
    parts = (core.group(0) if core else "0").split(".")
    return ".".join(parts + ["0"] * (4 - len(parts)))



ISS = ROOT / "dist_assets" / "win" / "eve-booty.iss"


def find_iscc() -> Path | None:
    r"""The Inno Setup compiler, wherever the installer put it.

    winget installs it per-user under %LOCALAPPDATA%\Programs, choco and the
    normal installer put it under Program Files. Looking in only one of those
    is how you get "not installed" on a machine where it plainly is.
    """
    roots = [
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("ProgramFiles", ""),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"),
    ]
    for root in filter(None, roots):
        candidate = Path(root) / "Inno Setup 6" / "ISCC.exe"
        if candidate.exists():
            return candidate
    return None


def build_installer(output: Path, app_version: str, file_version: str) -> int:
    r"""Stage the program folder where the .iss expects it, then compile.

    The script reads dist\EVEBooty, while Nuitka writes <output>/EVEBooty.dist,
    so the folder is copied rather than the script taught about both -- CI
    stages to the same path, and one layout is easier to reason about than a
    conditional one. update.exe is copied in too when it has been built,
    because the installed app needs it to update itself later.
    """
    if sys.platform != "win32":
        print("the installer is Windows-only; skipping", file=sys.stderr)
        return 0
    iscc = find_iscc()
    if iscc is None:
        print(
            "ISCC.exe not found. Install it with:\n"
            "  winget install --id JRSoftware.InnoSetup -e",
            file=sys.stderr,
        )
        return 1

    built = ROOT / output / "EVEBooty.dist"
    if not built.is_dir():
        print(f"no program folder at {built}", file=sys.stderr)
        return 1

    staged = ROOT / "dist" / "EVEBooty"
    if staged.exists():
        shutil.rmtree(staged)
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(built, staged)

    helper = ROOT / "updater" / "target" / "release" / "update.exe"
    if helper.exists():
        shutil.copy2(helper, staged / "update.exe")
    else:
        print(
            "warning: updater/target/release/update.exe is missing, so the "
            "installed app will not be able to update itself. Build it with:\n"
            "  cargo build --release --manifest-path updater/Cargo.toml",
            file=sys.stderr,
        )

    cmd = [
        str(iscc),
        f"/DAppVersion={app_version}",
        f"/DFileVersion={file_version}",
        str(ISS),
    ]
    print(" ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--onefile", action="store_true", help="produce a single exe")
    parser.add_argument("--console", action="store_true", help="keep the console window")
    parser.add_argument("--output", default="dist", help="output directory")
    parser.add_argument(
        "--show-bloat", action="store_true",
        help="report what anti-bloat stripped, to check nothing needed was",
    )
    parser.add_argument(
        "--installer", action="store_true",
        help="also build the Inno Setup installer (needs ISCC.exe)",
    )
    args = parser.parse_args()

    app_version = version()
    file_version = numeric(app_version)

    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        "--enable-plugin=pyside6",
        "--include-package=evasset",
        # the app icon lives beside the package and is loaded at runtime
        "--include-package-data=evasset",
        # keyring finds its OS backend by entry point, which Nuitka cannot see
        "--include-package=keyring.backends",
        "--include-package=jaraco",
        # Nuitka's anti-bloat plugin is auto-enabled, but its default mode is
        # "warning": it spots a library dragging in setuptools, pytest,
        # unittest, pydoc, IPython, dask or numba and then compiles it in
        # anyway. nofollow is what actually drops them. None are reachable
        # from this app -- the ones that appear at all arrive as incidental
        # imports inside dependencies.
        "--noinclude-default-mode=nofollow",
        # Not covered by anti-bloat, and PySide6 has no use for it.
        "--nofollow-import-to=tkinter",
        f"--output-dir={args.output}",
        "--output-filename=evebooty",
        # Without this the program folder is named after the entry point
        # *file*, so it comes out as "__main__.dist" -- which says nothing
        # about what it contains and is not something to hand anyone.
        # --output-filename only names the exe inside it.
        "--output-folder-name=EVEBooty",
        "--company-name=LittlePhish",
        "--product-name=EVE Booty",
        f"--product-version={file_version}",
        f"--file-version={file_version}",
        "--file-description=EVE Booty - EVE Online asset manager",
    ]
    if args.show_bloat:
        cmd.append("--show-anti-bloat-changes")
    if args.onefile:
        cmd.append("--onefile")
    if sys.platform == "win32" and not args.console:
        cmd.append("--windows-console-mode=disable")
    icon = ROOT / "src" / "evasset" / "assets" / "booty.ico"
    if icon.exists() and sys.platform == "win32":
        cmd.append(f"--windows-icon-from-ico={icon}")
    cmd.append(str(ENTRY))

    print(" ".join(cmd), flush=True)
    code = subprocess.call(cmd, cwd=ROOT)
    if code or not args.installer:
        return code
    return build_installer(Path(args.output), app_version, file_version)


if __name__ == "__main__":
    raise SystemExit(main())
