"""The tag -> version mapping the release build is stamped from.

This is build tooling rather than app code, but it decides what every shipped
exe claims to be and what the in-app updater compares against, so it is worth
the same pinning down as the updater itself. A wrong answer here is not a
cosmetic bug: a build stamped lower than it should be offers every user an
endless update, and one stamped higher hides the next real release from them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import evasset
from evasset import config, updater

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    """scripts/ is not a package and is not on the path; load it by file."""
    path = ROOT / "scripts" / "set_version.py"
    spec = importlib.util.spec_from_file_location("set_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


set_version = _load_script()


@pytest.mark.parametrize(
    "tag,version,file_version",
    [
        ("v1.2.3", "1.2.3", "1.2.3.0"),
        ("1.2.3", "1.2.3", "1.2.3.0"),          # the v is optional
        ("v1.2", "1.2", "1.2.0.0"),
        ("v1.2.3.4", "1.2.3.4", "1.2.3.4"),
        ("v0.1.0", "0.1.0", "0.1.0.0"),
        ("v10.20.30", "10.20.30", "10.20.30.0"),
        ("  v1.2.3  ", "1.2.3", "1.2.3.0"),     # trailing newline off a shell
    ],
)
def test_a_tag_becomes_a_version_and_a_windows_file_version(tag, version, file_version):
    assert set_version.parse_tag(tag) == (version, file_version)


@pytest.mark.parametrize(
    "tag,version,file_version",
    [
        ("v1.2.3-rc1", "1.2.3-rc1", "1.2.3.0"),
        ("v1.2.3-beta.2", "1.2.3-beta.2", "1.2.3.0"),
        ("v1.2.3+build7", "1.2.3+build7", "1.2.3.0"),
    ],
)
def test_a_prerelease_suffix_survives_but_never_reaches_the_exe_resource(
    tag, version, file_version
):
    """VERSIONINFO is four integers; "rc1" cannot go in it. The suffix stays in
    __version__ so the About box is honest, and the numeric core goes to
    Windows."""
    assert set_version.parse_tag(tag) == (version, file_version)


@pytest.mark.parametrize(
    "tag",
    ["", "   ", "nightly", "v", "vX.Y.Z", "release-1.2.3", "1", "v1.2.3.4.5"],
)
def test_a_tag_that_is_not_a_version_is_refused(tag):
    """Not a silent fallback to 0.0.0: that would look older than every real
    release to updater.parse_version and offer everyone a perpetual update."""
    with pytest.raises(ValueError):
        set_version.parse_tag(tag)


def test_the_generated_file_is_importable_and_carries_the_version(tmp_path):
    source = set_version.render("1.2.3-rc1")
    path = tmp_path / "_version.py"
    path.write_text(source, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("generated_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.__version__ == "1.2.3-rc1"


def test_the_package_exposes_a_version():
    """__init__ re-exports it, which is what config.user_agent and the About
    box read. It used to be defined and then referenced nowhere."""
    assert isinstance(evasset.__version__, str)
    assert evasset.__version__


def test_a_source_checkout_says_it_is_not_a_release():
    """The committed _version.py must stay a .dev version. If a release stamp
    were ever committed by accident, every source run would start claiming to
    be that release."""
    assert evasset.__version__.startswith("0.0.0.dev")


def test_the_user_agent_identifies_the_build_and_a_real_project():
    """CCP uses this header to work out who to talk to before throttling a
    third-party tool. It used to be a hardcoded 0.1.0 and a github.com/local/
    URL that does not resolve."""
    agent = config.user_agent()
    assert evasset.__version__ in agent
    assert config.PROJECT_URL in agent
    assert "github.com/local" not in agent


def test_the_contact_email_is_appended_when_set():
    settings = config.Settings(contact_email="pilot@example.com")
    assert "contact:pilot@example.com" in config.user_agent(settings)


def test_the_project_url_and_the_update_repo_are_the_same_repository():
    """The User-Agent points at one repo and the updater pulls releases from
    another only if these drift; there is no reason for them to differ."""
    assert updater.UPDATE_REPO == config.PROJECT_REPO
    assert config.PROJECT_URL.endswith(config.PROJECT_REPO)


def test_version_string_is_just_the_stamped_version_from_source(monkeypatch):
    """current_version() is None off a frozen Windows build, so there is
    nothing to disagree with."""
    monkeypatch.setattr(updater, "current_version", lambda: None)
    assert updater.version_string() == evasset.__version__


def test_version_string_flags_a_build_whose_exe_resource_disagrees(monkeypatch):
    """The failure mode this exists to catch: the stamping step did not run,
    so the exe and the package claim different versions."""
    monkeypatch.setattr(updater, "current_version", lambda: "9.9.9.0")
    shown = updater.version_string()
    assert evasset.__version__ in shown
    assert "9.9.9.0" in shown


# ------------------------------------------------------- shipped ESI client id
def test_a_fresh_install_gets_the_shipped_client_id():
    """No settings file at all: the dataclass default applies."""
    assert config.Settings().client_id == config.DEFAULT_CLIENT_ID
    assert config.DEFAULT_CLIENT_ID


def test_the_shipped_application_has_no_secret():
    """PKCE only. A secret in a downloadable binary is extractable, so one must
    never ship -- this fails the build if somebody pastes one in."""
    assert config.Settings().client_secret == ""


def test_an_empty_saved_client_id_falls_back_to_the_default(tmp_path, monkeypatch):
    """Clearing the box in Settings writes "" rather than dropping the key, and
    a dataclass default only applies when the key is absent. Without the
    coercion in load(), client_id="" went to SSO and came back a 401.
    """
    import json

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"client_id": "", "callback_port": 8629}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", path)

    assert config.Settings.load().client_id == config.DEFAULT_CLIENT_ID


def test_whitespace_is_not_a_client_id(tmp_path, monkeypatch):
    import json

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"client_id": "   "}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", path)

    assert config.Settings.load().client_id == config.DEFAULT_CLIENT_ID


def test_a_users_own_client_id_still_wins(tmp_path, monkeypatch):
    """The whole point of the default is that it is replaceable."""
    import json

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"client_id": "my-own-application"}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", path)

    assert config.Settings.load().client_id == "my-own-application"
