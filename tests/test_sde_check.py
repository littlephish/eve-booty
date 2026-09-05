"""Deciding whether to offer a game data update, without downloading one.

The check is one 80-byte GET for CCP's current build number; accepting the
result is about 95 MB. Those costs are far enough apart that the question gets
asked at startup and the answer never assumed.

Two states look similar and must not be treated the same. A missing SDE is
broken, not stale: ASSET_ROWS inner joins sde_types, so nothing at all can be
displayed until it exists. An out-of-date SDE still works.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from evasset import db, sde


@pytest.fixture()
def conn(tmp_path):
    return db.init(tmp_path / "sde.sqlite")


# ----------------------------------------------------------------- the status
def test_a_missing_sde_is_reported_as_missing_not_stale():
    status = sde.SdeStatus(installed=None, latest=3494416)
    assert status.missing and status.needed
    assert not status.stale


def test_an_older_build_is_stale():
    status = sde.SdeStatus(installed=3458726, latest=3494416)
    assert status.stale and status.needed
    assert not status.missing


def test_the_current_build_needs_nothing():
    status = sde.SdeStatus(installed=3494416, latest=3494416)
    assert not status.needed


def test_a_build_newer_than_ccp_is_not_stale():
    """Should not happen, but treating it as stale would loop a user through
    a 95 MB download that changes nothing."""
    assert not sde.SdeStatus(installed=3494999, latest=3494416).needed


# ------------------------------------------------------------ how often to ask
def test_a_missing_sde_is_always_due(conn):
    """Even just checked. This state is broken, and every launch is a fair
    moment to say so."""
    db.set_meta(conn, sde.META_CHECKED_AT, datetime.now(timezone.utc).isoformat())
    assert sde.due_for_check(conn) is True


def test_a_recent_check_is_not_repeated(conn):
    db.set_meta(conn, "sde_build", "3494416")
    db.set_meta(conn, sde.META_CHECKED_AT, datetime.now(timezone.utc).isoformat())
    assert sde.due_for_check(conn) is False


def test_an_old_check_is_due_again(conn):
    db.set_meta(conn, "sde_build", "3494416")
    stale = datetime.now(timezone.utc) - sde.CHECK_INTERVAL - timedelta(minutes=1)
    db.set_meta(conn, sde.META_CHECKED_AT, stale.isoformat())
    assert sde.due_for_check(conn) is True


def test_a_corrupt_timestamp_does_not_wedge_the_check(conn):
    """A meta row edited by hand, or written by an older build, must not stop
    the app ever checking again."""
    db.set_meta(conn, "sde_build", "3494416")
    db.set_meta(conn, sde.META_CHECKED_AT, "not a date")
    assert sde.due_for_check(conn) is True


# ------------------------------------------------------------------- declining
def test_a_declined_build_is_remembered(conn):
    sde.skip_build(conn, 3494416)
    assert sde.was_skipped(conn, 3494416) is True


def test_declining_one_build_does_not_decline_the_next(conn):
    """"Not now" applies to what was offered, not to updates in general."""
    sde.skip_build(conn, 3494416)
    assert sde.was_skipped(conn, 3500000) is False


def test_check_records_when_it_ran(conn, monkeypatch):
    monkeypatch.setattr(sde, "latest_build", lambda settings=None: 3494416)
    db.set_meta(conn, "sde_build", "3458726")

    status = sde.check(conn)

    assert status.stale
    assert status.installed == 3458726 and status.latest == 3494416
    assert db.get_meta(conn, sde.META_CHECKED_AT)
    # ...and having checked, it does not immediately check again.
    assert sde.due_for_check(conn) is False
