"""A minimal 5-field cron expression matcher (no dependencies).

Standard syntax:  minute hour day-of-month month day-of-week

- ``*`` — every value; ``*/n`` — every n; ``a-b`` — range; ``a-b/n`` —
  stepped range; comma-separated lists of the above
- month and weekday names (``jan``..``dec``, ``sun``..``sat``) accepted
- 0 and 7 are both Sunday; names are case-insensitive
- Vixie-cron semantics for the two day fields: when BOTH day-of-month and
  day-of-week are restricted (neither is ``*``), a day matches if either
  field matches; otherwise all fields must match

Time is minute-granular and evaluated in the server's local timezone
(schedules fire at the wall-clock times the operator sees).

`next_after` steps day-by-day (never minute-by-minute), so even a
never-matching expression is decided in bounded time; the four-year
window covers the whole leap-year cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

FIELDS = ("minute", "hour", "day", "month", "dow")
RANGES = {"minute": (0, 59), "hour": (0, 23), "day": (1, 31),
          "month": (1, 12), "dow": (0, 7)}
MONTH_NAMES = {name: i + 1 for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}
DOW_NAMES = {name: i for i, name in enumerate(
    ["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
# Four years cover every date the leap-year cycle can produce, so a
# yearly pattern (Feb 29) resolves while a never-matching one (Feb 30)
# still terminates.
SEARCH_WINDOW_DAYS = 366 * 4


class CronError(ValueError):
    """An expression that is not valid (or matches nothing)."""


def _cron_dow(at: datetime) -> int:
    """Python's Monday=0..Sunday=6 → cron's Sunday=0..Saturday=6."""
    return (at.weekday() + 1) % 7


def _name_lookup(names: dict[str, int], token: str) -> int | None:
    """3-letter abbreviations, or a full name whose prefix is one (sun,
    sunday) — the standard cron niceties, case-insensitive."""
    if token in names:
        return names[token]
    if len(token) > 3 and token[:3] in names:
        return names[token[:3]]
    return None


@dataclass(frozen=True)
class CronSpec:
    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]           # 0-6, Sunday = 0
    day_restricted: bool          # day-of-month was given (not a bare *)
    dow_restricted: bool          # day-of-week was given (not a bare *)

    def matches(self, at: datetime) -> bool:
        """Whether the minute containing `at` fires (seconds ignored)."""
        if at.minute not in self.minute or at.hour not in self.hour \
                or at.month not in self.month:
            return False
        if self.day_restricted and self.dow_restricted:
            # classic cron: either day field matching is enough
            return at.day in self.day or _cron_dow(at) in self.dow
        return at.day in self.day and _cron_dow(at) in self.dow


def _parse_field(raw: str, field: str, names: dict[str, int] | None) -> frozenset[int]:
    lo, hi = RANGES[field]
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            raise CronError(f"empty list item in '{field}' field")
        step = 1
        stepped = "/" in part
        if stepped:
            part, _, step_raw = part.partition("/")
            if not step_raw.isdigit() or int(step_raw) < 1:
                raise CronError(f"bad step '{step_raw}' in '{field}' field")
            step = int(step_raw)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start = _name_lookup(names, a) if names and not a.isdigit() else (int(a) if a.isdigit() else None)
            end = _name_lookup(names, b) if names and not b.isdigit() else (int(b) if b.isdigit() else None)
            if start is None or end is None:
                raise CronError(f"bad range '{part}' in '{field}' field")
        elif stepped:
            # `a/n` steps from a to the field maximum (Vixie cron)
            if names and not part.isdigit():
                start = end = _name_lookup(names, part)
                if start is None:
                    raise CronError(f"unknown name '{part}' in '{field}' field")
            elif not part.isdigit():
                raise CronError(f"bad value '{part}' in '{field}' field")
            else:
                start = int(part)
            end = hi
        else:
            if names and not part.isdigit():
                start = end = _name_lookup(names, part)
                if start is None:
                    raise CronError(f"unknown name '{part}' in '{field}' field")
            elif not part.isdigit():
                raise CronError(f"bad value '{part}' in '{field}' field")
            else:
                start = end = int(part)
        if start < lo or end > hi or start > end:
            raise CronError(f"value out of range in '{field}' field ('{raw}')")
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError(f"'{field}' field matches nothing")
    return frozenset(values)


def parse_cron(expr: str) -> CronSpec:
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(
            "a cron expression needs 5 fields: minute hour day-of-month "
            "month day-of-week (e.g. '0 8 * * *' = daily at 08:00)")
    minute, hour, day, month, dow = (
        _parse_field(parts[i], f, n)
        for i, f, n in ((0, "minute", None), (1, "hour", None), (2, "day", None),
                        (3, "month", MONTH_NAMES), (4, "dow", DOW_NAMES)))
    dow = frozenset(0 if v == 7 else v for v in dow)  # 7 is Sunday too
    spec = CronSpec(minute, hour, day, month, dow,
                    # a field starting with '*' (incl. */n) counts as
                    # unrestricted for the day/day-of-week OR rule
                    day_restricted=not parts[2].startswith("*"),
                    dow_restricted=not parts[4].startswith("*"))
    if next_after(spec, datetime.now()) is None:
        raise CronError(f"expression '{expr}' matches no time in the "
                        f"next {SEARCH_WINDOW_DAYS // 366} years")
    return spec


def next_after(spec: CronSpec, after: datetime,
               window_days: int = SEARCH_WINDOW_DAYS) -> datetime | None:
    """The first firing strictly after `after`, or None within the window."""
    for offset in range(window_days + 1):
        date = after.date() + timedelta(days=offset)
        if date.month not in spec.month:
            continue
        dow = (date.weekday() + 1) % 7   # cron numbering: Sunday = 0
        if spec.day_restricted and spec.dow_restricted:
            if date.day not in spec.day and dow not in spec.dow:
                continue
        elif date.day not in spec.day or dow not in spec.dow:
            continue
        # hours/minutes: smallest matching time not earlier than the
        # first eligible minute of the window (after + 1min on day 0)
        start = after.replace(second=0, microsecond=0) + timedelta(minutes=1) \
            if offset == 0 else datetime.combine(date, datetime.min.time())
        if start.date() != date:      # after+1min rolled into the next day
            continue
        for hour in sorted(spec.hour):
            if hour < start.hour:
                continue
            for minute in sorted(spec.minute):
                if (hour, minute) >= (start.hour, start.minute):
                    return datetime.combine(
                        date, datetime.min.time()).replace(hour=hour, minute=minute)
    return None


def describe(expr: str) -> str:
    """A short human label for the common shapes; the raw expression
    otherwise. Never raises — a stored expression already parsed."""
    try:
        parts = expr.split()
        minute, hour, day, month, dow = parts
    except (ValueError, AttributeError):
        return expr
    if not (day == month == dow == "*"):
        return expr
    if minute.startswith("*/"):
        return f"every {minute[2:]} minutes"
    if hour == "*" or minute == "*":
        return expr
    try:
        if int(hour) == 0 and int(minute) == 0:
            return "daily at 00:00"
        return f"daily at {int(hour):02d}:{int(minute):02d}"
    except ValueError:
        return expr
