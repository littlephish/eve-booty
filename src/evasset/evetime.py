"""Reading and writing EVE's clock.

EVE runs on UTC and quotes every timer in it, so nothing here converts to
local time: a reinforcement exit is a wall-clock time fleets form up on, and
rendering it in the viewer's zone would mean everyone converting it back by
hand.

Qt-free and lifted out of ui/structures_view.py, which is where these grew:
the Structures tab needs them and so does evasset.structure_history, and that
module must not drag a GUI toolkit in behind it just to format a date.
structures_view still re-exports the names it used to define, so nothing that
imported them from there had to move.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def parse_utc(value) -> datetime | None:
    """ESI hands back ISO 8601 with a Z. Python did not accept Z in
    fromisoformat until 3.11 and this project supports 3.10."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fmt_eve(when: datetime | None) -> str:
    return "" if when is None else when.strftime("%Y-%m-%d %H:%M")


def fmt_remaining(delta: timedelta) -> str:
    """Coarse on purpose. Nobody schedules a fleet off the seconds column, and
    a value that changes every second is a value you cannot read."""
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "passed"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "now"


def fmt_deadline(value, now: datetime | None = None) -> str:
    when = parse_utc(value)
    if when is None:
        return ""
    now = now or datetime.now(timezone.utc)
    return f"{fmt_eve(when)}  ·  {fmt_remaining(when - now)}"


def sort_key(value) -> float:
    """Sort deadline columns chronologically. Empty sorts last rather than
    first -- a structure with no timer is not the most urgent thing on screen."""
    when = parse_utc(value)
    return float("inf") if when is None else when.timestamp()


