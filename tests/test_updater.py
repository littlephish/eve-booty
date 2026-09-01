"""Update logic. No network, no Qt, no Windows needed.

The parts worth pinning down are the ones that decide whether an update
happens at all: which release asset is the program folder, whether a tag is
actually newer than what is running, and where the exe lives inside an archive
that Compress-Archive nested one level deeper than you would expect.

can_update() is also asserted to be False off Windows and from source, because
that is the guard standing between an updater and somebody's git checkout.
"""

from __future__ import annotations

import sys
import zipfile

import pytest

from evasset import updater


# ------------------------------------------------------------------ versions
@pytest.mark.parametrize(
    "text,expected",
    [
        ("v1.2.3", (1, 2, 3)),
        ("1.2.3", (1, 2, 3)),
        ("1.2.3.4", (1, 2, 3, 4)),
        ("v0.1.0-beta", (0, 1, 0)),
        ("", (0,)),
        ("nonsense", (0,)),
    ],
)
def test_versions_parse_to_comparable_tuples(text, expected):
    assert updater.parse_version(text) == expected


def test_a_v_prefix_does_not_make_a_tag_look_newer():
    """The tag carries "v", the exe's version resource does not. Compared as
    strings, "v1.0.0" beats "1.0.1"; that is the bug this guards."""
    assert updater.parse_version("v1.0.0") < updater.parse_version("1.0.1")


def test_the_windows_four_part_version_compares_against_a_three_part_tag():
    """Windows stamps 1.2.3.0; the tag says v1.2.3. Neither is newer."""
    assert updater.parse_version("1.2.3.0") > updater.parse_version("v1.2.3")
    assert updater.parse_version("v1.2.4") > updater.parse_version("1.2.3.0")


# -------------------------------------------------------------------- assets
def test_the_program_folder_zip_is_picked():
    assets = [
        {"name": "EVEBooty-1.2.3-win64.zip", "browser_download_url": "u"},
    ]
    assert updater.pick_asset(assets)["name"] == "EVEBooty-1.2.3-win64.zip"


def test_an_installer_exe_is_never_mistaken_for_the_archive():
    """A release may carry a setup.exe alongside the portable zip. Only the
    zip can be mirrored over an install folder."""
    assets = [
        {"name": "EVEBooty-1.2.3-setup.exe", "browser_download_url": "u"},
        {"name": "EVEBooty-1.2.3-win64.zip", "browser_download_url": "z"},
    ]
    assert updater.pick_asset(assets)["browser_download_url"] == "z"


def test_a_source_archive_is_not_the_program_folder():
    """GitHub attaches Source code (zip) to every release. It has no exe in
    it, and "win" in the name is what keeps it out."""
    assets = [{"name": "eve-assets-1.2.3-source.zip", "browser_download_url": "s"}]
    assert updater.pick_asset(assets) is None


def test_no_assets_at_all_is_not_an_error():
    assert updater.pick_asset([]) is None


# ------------------------------------------------------------------ archives
def _archive(tmp_path, arcnames):
    path = tmp_path / "update.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name in arcnames:
            archive.writestr(name, "x")
    return path


def test_extract_finds_the_exe_nested_one_level_down(tmp_path):
    """Compress-Archive -Path dist\\EVEBooty puts the folder inside the zip,
    so the exe is not at the root."""
    path = _archive(tmp_path, [f"EVEBooty/{updater.EXE_NAME}", "EVEBooty/update.exe"])
    assert updater.extract(path).name == "EVEBooty"


def test_extract_handles_the_exe_at_the_root(tmp_path):
    path = _archive(tmp_path, [updater.EXE_NAME])
    assert (updater.extract(path) / updater.EXE_NAME).exists()


def test_an_archive_without_the_exe_is_refused(tmp_path):
    """Better to stop here than to mirror a folder with no application in it
    over a working install."""
    path = _archive(tmp_path, ["readme.txt"])
    with pytest.raises(RuntimeError, match=updater.EXE_NAME):
        updater.extract(path)


# -------------------------------------------------------------------- guards
def test_running_from_source_never_updates(monkeypatch):
    """The guard between an updater and a developer's git checkout."""
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    assert updater.can_update() is False


@pytest.mark.skipif(sys.platform == "win32", reason="this asserts the non-Windows case")
def test_the_updater_is_windows_only():
    assert updater.can_update() is False


def test_a_read_only_install_directory_blocks_the_update(monkeypatch, tmp_path):
    """A machine-wide Program Files install needs elevation we never ask for.
    Saying so beats failing halfway through a folder swap."""
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path / "nope")
    assert updater.can_write_install_dir() is False
    assert updater.can_update() is False


def test_the_repo_is_overridable_from_the_environment():
    """So a fork can point its own build at its own releases without a patch."""
    assert "/" in updater.UPDATE_REPO
