"""One lapsed login must not take the whole sync down with it.

A refresh token EVE SSO will not exchange is a fact about one character. It
used to surface as an AuthError from whichever endpoint happened to touch the
token first, and because AuthError is a RuntimeError rather than an ESIError,
the per-step `except ESIError` in sync_character never saw it. The exception
escaped the character loop entirely, so a single stale login aborted the run
at whichever character sorted first -- and every character after it was
skipped without being named, leaving a traceback that said which endpoint had
failed but not which character owned the token.

This is not hypothetical: it is what happens to everyone the first time the
shipped ESI client id changes, because a refresh token belongs to the
application that issued it.
"""

from __future__ import annotations

import pytest

from evasset import db
from evasset.config import SCOPES as _CONFIG_SCOPES
from evasset.config import Settings
from evasset.esi.auth import AuthError
from evasset.esi.sync import Syncer, _auth_failure_reason

# Every character-side scope, so a healthy character produces no warnings at
# all and last_error stays empty -- otherwise these assertions cannot tell
# "nothing went wrong" from "a step was skipped for want of a scope".
SCOPES = " ".join(s for s in _CONFIG_SCOPES if "corporation" not in s)

INVALID_GRANT = (
    "EVE SSO rejected the request (400). Either the client ID in Settings is not "
    "valid for this application, or the saved logins were authorised under a "
    "different client ID.\n"
    '{"error":"invalid_grant","error_description":"Invalid refresh token. '
    'Character grant missing/expired."}'
)


@pytest.fixture()
def conn(tmp_path):
    c = db.init(tmp_path / "auth.sqlite")
    c.executemany(
        "INSERT INTO characters (character_id, name, scopes, enabled, include_corp)"
        " VALUES (?,?,?,1,0)",
        [
            (1, "Aaa Firstalpha", SCOPES),
            (2, "Bbb Secondbeta", SCOPES),
            (3, "Ccc Thirdgamma", SCOPES),
        ],
    )
    return c


class DeadTokenClient:
    """Raises AuthError for one character, works for the rest."""

    def __init__(self, dead: set[int]):
        self.dead = dead
        self.touched: list[int] = []

    def _check(self, character_id):
        self.touched.append(character_id)
        if character_id in self.dead:
            raise AuthError(INVALID_GRANT)

    def all_pages(self, path, character_id=None, **kw):
        self._check(character_id)
        return []

    def get(self, path, character_id=None, **kw):
        self._check(character_id)
        return {} if "wallet" not in path else 0.0

    def request(self, method, path, character_id=None, **kw):
        self._check(character_id)
        return {}


def _sync_all(conn, client):
    syncer = Syncer(conn, client, Settings())
    warnings = []
    for row in syncer.enabled_characters():
        warnings += syncer.sync_character(row)
    return warnings


def test_one_dead_token_does_not_stop_the_others(conn):
    """The character that fails sorts first, which is what made the old bug so
    effective: nothing after it ran at all."""
    client = DeadTokenClient(dead={1})

    warnings = _sync_all(conn, client)

    # The other two were still reached.
    assert 2 in client.touched
    assert 3 in client.touched
    # And the failure was reported rather than raised.
    assert any("Aaa Firstalpha" in w for w in warnings)


def test_the_failing_character_is_named(conn):
    """"I can't tell which" was the actual complaint. The traceback named an
    endpoint; it never named the character whose token was stale."""
    warnings = _sync_all(conn, DeadTokenClient(dead={2}))

    failed = [w for w in warnings if "sign-in" in w]
    assert len(failed) == 1
    assert failed[0].startswith("Bbb Secondbeta:")


def test_the_reason_is_recorded_against_the_character(conn):
    """last_error is what the Characters dialog shows in Last result, so the
    answer survives the log pane scrolling away."""
    _sync_all(conn, DeadTokenClient(dead={3}))

    rows = {r["character_id"]: r["last_error"] for r in conn.execute("SELECT * FROM characters")}
    assert rows[3] and "sign-in" in rows[3]
    assert not rows[1]
    assert not rows[2]


def test_every_character_can_fail_without_raising(conn):
    """The all-stale case, which is exactly what changing the client id
    produces. It must still come back as three reports, not one exception."""
    warnings = _sync_all(conn, DeadTokenClient(dead={1, 2, 3}))

    assert len([w for w in warnings if "sign-in" in w]) == 3


def test_a_healthy_character_records_no_error(conn):
    _sync_all(conn, DeadTokenClient(dead=set()))

    errors = [r["last_error"] for r in conn.execute("SELECT * FROM characters")]
    assert not any(errors)


# ----------------------------------------------------------------- the message
def test_invalid_grant_explains_the_client_id_change():
    """The cause is invisible from Settings -- the client id there is correct,
    it is simply not the one that minted the stored token."""
    reason = _auth_failure_reason(AuthError(INVALID_GRANT))

    assert "re-add" in reason.lower()
    assert "client id" in reason.lower()


def test_another_auth_failure_still_says_what_to_do():
    reason = _auth_failure_reason(AuthError("Token endpoint returned 503: upstream down"))

    assert "re-add" in reason.lower()
    # The underlying detail survives, on one line rather than as a page of HTML.
    assert "503" in reason
    assert "\n" not in reason
