"""Tiny but honest cron: 5-field (minute hour day-of-month month day-of-week).

Supports `*`, lists `a,b`, ranges `a-b`, steps `*/n` and `a-b/n`. No seconds field,
no aliases — determinism beats sugar. Validated at workflow submission.
"""
from __future__ import annotations

import datetime as dt

FIELDS = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day": (1, 31),
    "month": (1, 12),
    "weekday": (0, 6),
}


def _parse_field(spec: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            step = int(step_s)
            if step <= 0:
                raise ValueError("step must be > 0")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if not (lo <= start <= end <= hi):
            raise ValueError(f"field value out of range {lo}-{hi}: {part}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError("empty cron field")
    return frozenset(values)


class CronSchedule:
    def __init__(self, expr: str):
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError("cron must have exactly 5 fields: minute hour dom month dow")
        self.expr = expr
        self._minute = _parse_field(parts[0], *FIELDS["minute"])
        self._hour = _parse_field(parts[1], *FIELDS["hour"])
        self._day = _parse_field(parts[2], *FIELDS["day"])
        self._month = _parse_field(parts[3], *FIELDS["month"])
        self._weekday = _parse_field(parts[4], *FIELDS["weekday"])

    def matches(self, moment: dt.datetime) -> bool:
        wd = moment.weekday()  # 0 = Monday; cron convention 0 = Sunday
        cron_wd = (wd + 1) % 7
        return (
            moment.minute in self._minute
            and moment.hour in self._hour
            and moment.day in self._day
            and moment.month in self._month
            and cron_wd in self._weekday
        )

    def next_after(self, now: dt.datetime) -> dt.datetime:
        """Next matching minute strictly after `now`."""
        candidate = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        for _ in range(60 * 24 * 366 * 2):  # bounded search (~2 years)
            if self.matches(candidate):
                return candidate
            candidate += dt.timedelta(minutes=1)
        raise ValueError("cron schedule did not match within 2 years (impossible expression)")
