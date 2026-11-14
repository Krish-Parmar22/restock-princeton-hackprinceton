"""Daily reorder-plan scheduler: pure 'is it time / already ran today' logic. No DB, no agent."""
from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from app.agent.daily_plan import DailyPlan, initial_last_run, is_due, parse_plan_time, start
from app.ledger.impl import TZ

AT = time(9, 0)


def ny(d: int, h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, d, h, m, tzinfo=TZ)


def test_parse_plan_time():
    assert parse_plan_time("09:00") == time(9, 0)
    assert parse_plan_time(" 17:30 ") == time(17, 30)
    assert parse_plan_time("") is None and parse_plan_time(None) is None
    for bad in ("9", "25:00", "nine:00"):
        with pytest.raises(ValueError):
            parse_plan_time(bad)


def test_is_due():
    assert not is_due(ny(14, 8, 59), AT, None)              # not yet
    assert is_due(ny(14, 9, 0), AT, None)                   # on the minute
    assert is_due(ny(14, 9, 0), AT, date(2026, 11, 13))     # ran yesterday
    assert not is_due(ny(14, 9, 1), AT, date(2026, 11, 14))  # already ran today
    assert is_due(ny(15, 9, 0), AT, date(2026, 11, 14))     # next day fires again


def test_is_due_uses_shop_local_time():
    # 13:30 UTC on Nov 14 = 08:30 EST: not yet. 14:00 UTC = 09:00 EST: due.
    assert not is_due(datetime(2026, 11, 14, 13, 30, tzinfo=timezone.utc), AT, None)
    assert is_due(datetime(2026, 11, 14, 14, 0, tzinfo=timezone.utc), AT, None)
    # 03:00 UTC Nov 15 is still Nov 14 22:00 in New York: same shop day as a 09:00 run.
    assert not is_due(datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc), AT, date(2026, 11, 14))


def test_start_after_plan_time_waits_for_tomorrow():
    assert initial_last_run(ny(14, 15), AT) == date(2026, 11, 14)
    assert initial_last_run(ny(14, 8), AT) is None


def test_tick_fires_once_per_day_and_survives_errors():
    now = {"t": ny(14, 8)}
    calls = []

    def boom():
        calls.append(now["t"])
        raise RuntimeError("bridge down")

    job = DailyPlan(AT, send=boom, clock=lambda: now["t"])
    assert job.tick() is False                 # 08:00
    now["t"] = ny(14, 9)
    assert job.tick() is True                  # fires; exception is swallowed
    now["t"] = ny(14, 9, 1)
    assert job.tick() is False                 # not twice the same day (even after a failure)
    now["t"] = ny(15, 9, 5)
    assert job.tick() is True
    assert len(calls) == 2


def test_start_disabled_or_invalid_returns_none():
    assert start("") is None
    assert start("not a time") is None
