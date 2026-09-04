"""Carrying an install across the evasset -> eve-booty rename.

APP_NAME picked three things at once: the data directory, the cache directory
and the keyring service holding every refresh token. Renaming it without
moving all three stranded the database in a folder nothing looked in and
silently logged out every character, which is why the old name was left in
place for so long. These tests pin the migration that made the rename safe.

The directory move is exercised against real directories rather than mocks --
the failure being guarded against is data loss, and a mock cannot tell you
whether the bytes arrived.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evasset import config


# ------------------------------------------------------------ directory move
def test_a_legacy_directory_is_adopted(tmp_path):
    legacy = tmp_path / "evasset" / "evasset"
    legacy.mkdir(parents=True)
    (legacy / "evasset.sqlite").write_bytes(b"database")
    (legacy / "settings.json").write_text('{"client_id": "mine"}', encoding="utf-8")

    current = tmp_path / "eve-booty" / "eve-booty"
    assert config._adopt_legacy_dir(current, legacy) is True

    assert (current / "evasset.sqlite").read_bytes() == b"database"
    assert json.loads((current / "settings.json").read_text())["client_id"] == "mine"
    assert not legacy.exists()


def test_the_wal_set_moves_with_the_database(tmp_path):
    """A -wal file holds committed transactions the main file does not have
    yet. Moving one without the other is how a rename becomes corruption."""
    legacy = tmp_path / "evasset" / "evasset"
    legacy.mkdir(parents=True)
    for name in ("evasset.sqlite", "evasset.sqlite-wal", "evasset.sqlite-shm"):
        (legacy / name).write_bytes(name.encode())

    current = tmp_path / "eve-booty" / "eve-booty"
    assert config._adopt_legacy_dir(current, legacy) is True

    for name in ("evasset.sqlite", "evasset.sqlite-wal", "evasset.sqlite-shm"):
        assert (current / name).read_bytes() == name.encode()


def test_an_existing_install_is_never_overwritten(tmp_path):
    """Both directories present means this install already lives at the new
    path. The old one is left alone rather than clobbering live data."""
    legacy = tmp_path / "evasset" / "evasset"
    legacy.mkdir(parents=True)
    (legacy / "evasset.sqlite").write_bytes(b"old")

    current = tmp_path / "eve-booty" / "eve-booty"
    current.mkdir(parents=True)
    (current / "evasset.sqlite").write_bytes(b"current")

    assert config._adopt_legacy_dir(current, legacy) is False
    assert (current / "evasset.sqlite").read_bytes() == b"current"
    assert (legacy / "evasset.sqlite").read_bytes() == b"old"


def test_an_empty_new_directory_does_not_block_the_move(tmp_path):
    """A previous launch that created the folder and then failed must not
    strand the data forever."""
    legacy = tmp_path / "evasset" / "evasset"
    legacy.mkdir(parents=True)
    (legacy / "evasset.sqlite").write_bytes(b"database")

    current = tmp_path / "eve-booty" / "eve-booty"
    current.mkdir(parents=True)  # empty

    assert config._adopt_legacy_dir(current, legacy) is True
    assert (current / "evasset.sqlite").read_bytes() == b"database"


def test_nothing_to_migrate_is_not_an_error(tmp_path):
    assert config._adopt_legacy_dir(tmp_path / "new", tmp_path / "missing") is False


def test_an_empty_legacy_directory_is_ignored(tmp_path):
    legacy = tmp_path / "evasset" / "evasset"
    legacy.mkdir(parents=True)
    assert config._adopt_legacy_dir(tmp_path / "new", legacy) is False


def test_an_explicit_data_dir_is_never_migrated_into(tmp_path, monkeypatch):
    """The override is what the test suite and portable installs use. Moving a
    real install's data into a scratch directory would be the worst bug
    available here, so the override short-circuits before any migration."""
    monkeypatch.setenv("EVEBOOTY_DATA_DIR", str(tmp_path / "scratch"))
    called = []
    monkeypatch.setattr(config, "_adopt_legacy_dir", lambda *a: called.append(a) or False)

    resolved = config._resolve_dir("DATA_DIR", str(tmp_path / "ignored"), "user_data_dir")

    assert resolved == tmp_path / "scratch"
    assert called == []


def test_the_legacy_env_var_still_works(tmp_path, monkeypatch):
    """Someone's scheduled task or shell profile should not break on upgrade."""
    monkeypatch.delenv("EVEBOOTY_DATA_DIR", raising=False)
    monkeypatch.setenv("EVASSET_DATA_DIR", str(tmp_path / "old-style"))
    assert config._env("DATA_DIR") == str(tmp_path / "old-style")


def test_the_current_env_var_wins_over_the_legacy_one(tmp_path, monkeypatch):
    monkeypatch.setenv("EVEBOOTY_DATA_DIR", str(tmp_path / "new"))
    monkeypatch.setenv("EVASSET_DATA_DIR", str(tmp_path / "old"))
    assert config._env("DATA_DIR") == str(tmp_path / "new")


# ------------------------------------------------------------- keyring move
class FakeKeyring:
    """Enough of the keyring API to test the token hand-over, including the
    enumeration it deliberately does not rely on."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def get_password(self, service, key):
        return self.store.get((service, key))

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def delete_password(self, service, key):
        del self.store[(service, key)]


@pytest.fixture
def fake_keyring(monkeypatch):
    from evasset.esi import auth

    fake = FakeKeyring()
    monkeypatch.setattr(auth, "keyring", fake)
    monkeypatch.setattr(auth, "_keyring_available", lambda: True)
    return fake


def test_a_token_stored_under_the_old_service_is_found_and_moved(fake_keyring):
    from evasset.esi import auth

    fake_keyring.store[("evasset-refresh-token", "12345")] = "refresh-me"

    assert auth.load_refresh_token(12345) == "refresh-me"
    # Adopted under the current name...
    assert fake_keyring.store[(auth.KEYRING_SERVICE, "12345")] == "refresh-me"
    # ...and the old entry cleaned up, so this happens once.
    assert ("evasset-refresh-token", "12345") not in fake_keyring.store


def test_a_current_token_is_returned_without_touching_the_legacy_store(fake_keyring):
    from evasset.esi import auth

    fake_keyring.store[(auth.KEYRING_SERVICE, "42")] = "current"
    fake_keyring.store[("evasset-refresh-token", "42")] = "stale"

    assert auth.load_refresh_token(42) == "current"
    # The stale entry is not consulted, so it is not deleted either.
    assert fake_keyring.store[("evasset-refresh-token", "42")] == "stale"


def test_no_token_anywhere_returns_none(fake_keyring):
    from evasset.esi import auth

    assert auth.load_refresh_token(999) is None


def test_a_failing_delete_still_yields_the_token(fake_keyring, monkeypatch):
    """Cleanup is best-effort. Losing a login because the tidy-up failed would
    be a far worse trade than leaving a duplicate entry behind."""
    from evasset.esi import auth

    fake_keyring.store[("evasset-refresh-token", "7")] = "token"

    def boom(service, key):
        raise RuntimeError("credential store is locked")

    monkeypatch.setattr(fake_keyring, "delete_password", boom)

    assert auth.load_refresh_token(7) == "token"
    assert fake_keyring.store[(auth.KEYRING_SERVICE, "7")] == "token"


def test_the_legacy_service_names_are_derived_from_the_legacy_app_names():
    from evasset.esi import auth

    assert "evasset-refresh-token" in auth.LEGACY_KEYRING_SERVICES
    assert auth.KEYRING_SERVICE == "eve-booty-refresh-token"


def test_a_failed_move_keeps_using_the_old_directory(tmp_path, monkeypatch):
    """The failure mode that matters. If the rename cannot happen, the install
    must keep reading the directory the data is actually in -- starting empty
    beside it looks identical to every character and asset having vanished.
    """
    legacy = tmp_path / "evasset" / "evasset"
    legacy.mkdir(parents=True)
    (legacy / "evasset.sqlite").write_bytes(b"database")
    current = tmp_path / "eve-booty" / "eve-booty"

    monkeypatch.setattr(config, "_adopt_legacy_dir", lambda *a: False)
    monkeypatch.setattr(
        config, "PlatformDirs",
        lambda *a, **k: type("D", (), {"user_data_dir": str(legacy)})(),
    )
    monkeypatch.delenv("EVEBOOTY_DATA_DIR", raising=False)
    monkeypatch.delenv("EVASSET_DATA_DIR", raising=False)

    resolved = config._resolve_dir("DATA_DIR", str(current), "user_data_dir")
    assert resolved == legacy
    assert (resolved / "evasset.sqlite").read_bytes() == b"database"


def test_a_directory_move_works_on_windows(tmp_path):
    """os.replace passes MOVEFILE_REPLACE_EXISTING, which Windows rejects for
    directories with WinError 5 -- a permissions error that is not one. This
    is the regression test for that; it moved zero bytes on the first run."""
    legacy = tmp_path / "evasset" / "evasset"
    legacy.mkdir(parents=True)
    (legacy / "evasset.sqlite").write_bytes(b"x" * 1024)

    current = tmp_path / "eve-booty" / "eve-booty"
    assert config._adopt_legacy_dir(current, legacy) is True
    assert (current / "evasset.sqlite").stat().st_size == 1024


def test_a_half_finished_move_is_undone(tmp_path, monkeypatch):
    """shutil.move falls back to copy-then-delete, so a failure part way
    through leaves data in both places -- and the copy is a snapshot of a
    database that was open, so it may be torn. Left behind, the next launch
    would take that copy as authoritative and read a damaged database while
    the real one carried on being written next door. A running instance
    holding the file causes exactly this.
    """
    legacy = tmp_path / "evasset" / "evasset"
    legacy.mkdir(parents=True)
    (legacy / "evasset.sqlite").write_bytes(b"the real database")

    current = tmp_path / "eve-booty" / "eve-booty"

    def copy_then_fail(src, dst):
        Path(dst).mkdir(parents=True)
        (Path(dst) / "evasset.sqlite").write_bytes(b"torn partial copy")
        raise PermissionError("[WinError 32] used by another process")

    monkeypatch.setattr(config.shutil, "move", copy_then_fail)
    monkeypatch.setattr(config.os, "rename", lambda *a: (_ for _ in ()).throw(OSError()))

    assert config._adopt_legacy_dir(current, legacy) is False
    # The partial copy is gone, so nothing can mistake it for the real thing.
    assert not current.exists()
    # And the original is untouched.
    assert (legacy / "evasset.sqlite").read_bytes() == b"the real database"
