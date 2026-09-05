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
    monkeypatch.setattr(sde, "latest_build", lambda *a, **kw: 3494416)
    db.set_meta(conn, "sde_build", "3458726")

    status = sde.check(conn)

    assert status.stale
    assert status.installed == 3458726 and status.latest == 3494416
    assert db.get_meta(conn, sde.META_CHECKED_AT)
    # ...and having checked, it does not immediately check again.
    assert sde.due_for_check(conn) is False


# ------------------------------------------------------- bounded and optional
def test_a_missing_sde_needs_no_network(conn, monkeypatch):
    """The case that matters is settled locally. Asking CCP for a build number
    nobody will read only adds a way for the answer to arrive late."""
    def explode(*a, **kw):
        raise AssertionError("the network must not be touched here")

    monkeypatch.setattr(sde, "latest_build", explode)

    status = sde.check(conn)

    assert status.missing and status.needed
    assert status.latest is None


def test_a_slow_or_dead_endpoint_does_not_raise(conn, monkeypatch):
    """A startup check that throws would surface as an error dialog for
    something entirely optional."""
    import httpx

    db.set_meta(conn, "sde_build", "3458726")

    def timeout(*a, **kw):
        raise httpx.ConnectTimeout("too slow")

    monkeypatch.setattr(sde, "latest_build", timeout)

    status = sde.check(conn, timeout=sde.STARTUP_TIMEOUT)

    assert status.installed == 3458726
    assert status.latest is None
    assert not status.needed, "unknown must not mean out of date"


def test_a_failed_check_is_retried_rather_than_recorded(conn, monkeypatch):
    """Not stamping the timestamp on failure is what makes the next launch try
    again instead of waiting out the interval on an answer never received."""
    import httpx

    db.set_meta(conn, "sde_build", "3458726")
    monkeypatch.setattr(
        sde, "latest_build", lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("x"))
    )

    sde.check(conn, timeout=sde.STARTUP_TIMEOUT)

    assert db.get_meta(conn, sde.META_CHECKED_AT) is None
    assert sde.due_for_check(conn) is True


def test_the_startup_budget_is_bounded_but_reachable(conn):
    """Bounded, because an unbounded call parks a pool thread for as long as
    CCP takes. Not tight, because a cold connection is about four round trips
    before the server does anything, which is a full second of floor on a
    250ms RTT link -- a shorter budget would fail exactly the users furthest
    from CCP."""
    assert 3.0 <= sde.STARTUP_TIMEOUT <= 10.0


def test_an_unknown_latest_build_is_never_offered_as_an_update():
    assert not sde.SdeStatus(installed=3458726, latest=None).needed
