"""The 5-field cron matcher: parsing, matching, next-fire, rejection.

Semantics locked in here (see services/cron.py's docstring): Vixie-style
`a/n` steps, day/day-of-week OR rule, 3-letter or full English names,
Sunday = 0 and 7, strictly-after next-fire, and a bounded search window
that still resolves leap-day schedules.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.cron import CronError, describe, next_after, parse_cron

# 2026-11-03 is a Tuesday
T = datetime(2026, 11, 3, 14, 7, 30)


def next_fire(expr, after=T):
    return next_after(parse_cron(expr), after)


# --- parsing + next fire --------------------------------------------------

@pytest.mark.parametrize("expr,expected", [
    ("*/5 * * * *", datetime(2026, 11, 3, 14, 10)),
    ("30 * * * *", datetime(2026, 11, 3, 14, 30)),      # 14:30 is still after 14:07
    ("0 8 * * *", datetime(2026, 11, 4, 8, 0)),
    ("59 23 * * *", datetime(2026, 11, 3, 23, 59)),
    ("7 14 * * *", datetime(2026, 11, 4, 14, 7)),       # strictly after, same time next day
    ("0-20/10 * * * *", datetime(2026, 11, 3, 14, 10)),  # 0,10,20
    ("5/15 * * * *", datetime(2026, 11, 3, 14, 20)),     # Vixie: 5,20,35,50
    ("0 9,17 * * *", datetime(2026, 11, 3, 17, 0)),
    ("0 8 * * mon-fri", datetime(2026, 11, 4, 8, 0)),   # Wednesday
    ("0 8 * * 2", datetime(2026, 11, 10, 8, 0)),        # next Tuesday
    ("0 8 * * sunday", datetime(2026, 11, 8, 8, 0)),    # full name accepted
    ("0 8 * * 0", datetime(2026, 11, 8, 8, 0)),        # Sunday = 0
    ("0 8 * * 7", datetime(2026, 11, 8, 8, 0)),         # ...and = 7
    ("0 0 1 jan *", datetime(2027, 1, 1, 0, 0)),
    ("0 0 1 1 *", datetime(2027, 1, 1, 0, 0)),
    ("0 0 29 2 *", datetime(2028, 2, 29, 0, 0)),        # leap-day resolves in-window
    ("0 0 1 * sunday", datetime(2026, 11, 8, 0, 0)),    # OR rule: first Sunday beats Dec 1
    ("0 0 * * sunday", datetime(2026, 11, 8, 0, 0)),
])
def test_next_fire(expr, expected):
    assert next_fire(expr) == expected


def test_next_fire_is_strictly_after():
    # a fire at the queried minute itself does not count
    assert next_fire("30 * * * *", datetime(2026, 11, 3, 14, 30, 59)) \
        == datetime(2026, 11, 3, 15, 30)


def test_matches_uses_cron_day_numbering():
    assert parse_cron("0 8 * * 2").matches(datetime(2026, 11, 3, 8, 0))    # Tuesday
    assert not parse_cron("0 8 * * 2").matches(datetime(2026, 11, 4, 8, 0))
    assert parse_cron("0 8 * * sun").matches(datetime(2026, 11, 8, 8, 0))
    assert parse_cron("0 8 * * 0").matches(datetime(2026, 11, 8, 8, 0))


# --- rejection ------------------------------------------------------------

@pytest.mark.parametrize("expr", [
    "* * * *",          # four fields
    "a b c d e",        # five non-numeric fields
    "61 * * * *",       # minute out of range
    "* 24 * * *",       # hour out of range
    "0 0 32 * *",       # day out of range
    "0 0 0 * *",        # day zero
    "0 0 * * 13 *",     # six fields
    "0 0 * * 8",        # weekday 8
    "0 0 * * foo",      # unknown name
    "*/0 * * * *",      # zero step
    "*/x * * * *",      # non-numeric step
    "1,,2 * * * *",     # empty list item
    "0 0 30 2 *",       # Feb 30 never happens (within 4 years)
])
def test_rejects_bad_expressions(expr):
    with pytest.raises(CronError):
        parse_cron(expr)


def test_case_insensitive_names():
    assert next_fire("0 8 * * MON-FRI") == datetime(2026, 11, 4, 8, 0)
    assert next_fire("0 0 1 JAN *") == datetime(2027, 1, 1, 0, 0)


# --- describe -------------------------------------------------------------

@pytest.mark.parametrize("expr,text", [
    ("*/5 * * * *", "every 5 minutes"),
    ("*/15 * * * *", "every 15 minutes"),
    ("0 8 * * *", "daily at 08:00"),
    ("30 7 * * *", "daily at 07:30"),
    ("0 0 * * *", "daily at 00:00"),
    ("15 14 1 * *", "15 14 1 * *"),      # day-of-month set: no shortcut
    ("0 9,17 * * 1-5", "0 9,17 * * 1-5"),
])
def test_describe(expr, text):
    assert describe(expr) == text
