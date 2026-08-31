"""Minimal 5-field cron parser — no dependencies.

    ┌───── minute        0-59
    │ ┌─── hour          0-23
    │ │ ┌─ day of month  1-31
    │ │ │ ┌─ month       1-12  (or JAN-DEC)
    │ │ │ │ ┌─ weekday   0-6   (0 = Sunday, or SUN-SAT)
    * * * * *

Supported per field: `*`, `5`, `1,3,5`, `1-5`, `*/15`, `10-30/5`, and names.
Shorthands: @hourly @daily @midnight @weekly @monthly @yearly.

Deliberately *not* supported: `L`, `W`, `#`, `?`, seconds, years. Those raise
CronError rather than being silently ignored — a schedule that quietly does the
wrong thing is worse than one that refuses to save.

Times are evaluated in the server's local timezone.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

FIELDS = ("minute", "hour", "day", "month", "weekday")
RANGES = {"minute": (0, 59), "hour": (0, 23), "day": (1, 31),
          "month": (1, 12), "weekday": (0, 6)}

MONTHS = {n: i + 1 for i, n in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}
DAYS = {n: i for i, n in enumerate(
    ["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}

SHORTHAND = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

UNSUPPORTED = set("LW#?")


class CronError(ValueError):
    pass


def _parse_field(spec: str, name: str) -> Set[int]:
    lo, hi = RANGES[name]
    out: Set[int] = set()

    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{name}: empty value in '{spec}'")
        if any(c in UNSUPPORTED for c in part.upper()):
            raise CronError(f"{name}: '{part}' uses unsupported cron syntax")

        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"{name}: bad step '/{step_s}'")
            step = int(step_s)
            part = part or "*"

        if part == "*":
            start, end = lo, hi
        elif "-" in part.strip("-"):
            a, _, b = part.partition("-")
            start, end = _num(a, name), _num(b, name)
            if start > end:
                raise CronError(f"{name}: range '{part}' is backwards")
        else:
            start = end = _num(part, name)

        if start < lo or end > hi:
            raise CronError(f"{name}: '{part}' outside {lo}-{hi}")
        out.update(range(start, end + 1, step))

    if not out:
        raise CronError(f"{name}: '{spec}' matches nothing")
    return out


def _num(tok: str, name: str) -> int:
    tok = tok.strip().lower()
    if name == "month" and tok in MONTHS:
        return MONTHS[tok]
    if name == "weekday":
        if tok in DAYS:
            return DAYS[tok]
        if tok == "7":            # both 0 and 7 mean Sunday
            return 0
    if not tok.lstrip("-").isdigit():
        raise CronError(f"{name}: '{tok}' is not a number")
    return int(tok)


def parse(expr: str) -> Dict[str, Set[int]]:
    """Validate a cron expression and return the matching value sets."""
    if not isinstance(expr, str) or not expr.strip():
        raise CronError("empty cron expression")
    e = expr.strip().lower()
    e = SHORTHAND.get(e, e)
    if e.startswith("@"):
        raise CronError(f"unknown shorthand '{expr}' "
                        f"(try {', '.join(sorted(SHORTHAND))})")

    parts = e.split()
    if len(parts) != 5:
        raise CronError(f"expected 5 fields, got {len(parts)} — "
                        "format is 'minute hour day month weekday'")
    return {name: _parse_field(p, name) for name, p in zip(FIELDS, parts)}


def matches(expr: str, when: datetime) -> bool:
    f = parse(expr)
    if when.minute not in f["minute"] or when.hour not in f["hour"]:
        return False
    if when.month not in f["month"]:
        return False
    dom_restricted = len(f["day"]) < 31
    dow_restricted = len(f["weekday"]) < 7
    dom_ok = when.day in f["day"]
    dow_ok = (when.weekday() + 1) % 7 in f["weekday"]   # Mon=0 -> Sun=0
    # classic cron: when both day-of-month and weekday are restricted, either matches
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def next_run(expr: str, after: Optional[float] = None, horizon_days: int = 400) -> float:
    """Unix timestamp of the next firing strictly after `after`."""
    parse(expr)  # validate up front
    base = datetime.fromtimestamp(after if after is not None else time.time())
    cur = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = cur + timedelta(days=horizon_days)
    while cur < limit:
        if matches(expr, cur):
            return cur.timestamp()
        cur += timedelta(minutes=1)
    raise CronError(f"'{expr}' has no run in the next {horizon_days} days")


def describe(expr: str) -> str:
    """Short human summary — shown in the UI so a typo is obvious."""
    e = expr.strip().lower()
    if e in SHORTHAND:
        return {"@hourly": "every hour", "@daily": "every day at midnight",
                "@midnight": "every day at midnight", "@weekly": "every Sunday at midnight",
                "@monthly": "1st of every month at midnight",
                "@yearly": "1 January at midnight",
                "@annually": "1 January at midnight"}[e]
    f = parse(expr)
    m, h = f["minute"], f["hour"]
    if len(m) == 60 and len(h) == 24:
        base = "every minute"
    elif len(m) == 1 and len(h) == 24:
        base = f"every hour at :{min(m):02d}"
    elif len(m) > 1 and len(h) == 24:
        step = sorted(m)[1] - sorted(m)[0] if len(m) > 1 else 1
        base = f"every {step} minutes" if len(m) == 60 // max(step, 1) else \
               f"at minutes {','.join(str(x) for x in sorted(m)[:6])}"
    elif len(m) == 1 and len(h) == 1:
        base = f"daily at {min(h):02d}:{min(m):02d}"
    else:
        base = (f"at {','.join(f'{x:02d}' for x in sorted(h)[:4])}"
                f":{min(m):02d}")

    # "daily at X on Mon" reads wrong — it is weekly
    if len(f["weekday"]) < 7 and base.startswith("daily "):
        base = base.replace("daily ", "", 1)
    bits = [base]
    if len(f["weekday"]) < 7:
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        bits.append("on " + ",".join(names[d] for d in sorted(f["weekday"])))
    if len(f["day"]) < 31:
        bits.append("on day " + ",".join(str(d) for d in sorted(f["day"])[:6]))
    if len(f["month"]) < 12:
        mn = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        bits.append("in " + ",".join(mn[x - 1] for x in sorted(f["month"])))
    return " ".join(bits)


def preview(expr: str, count: int = 5) -> List[str]:
    """Next few firing times, for the UI."""
    out, t = [], time.time()
    for _ in range(count):
        t = next_run(expr, t)
        out.append(datetime.fromtimestamp(t).strftime("%a %d %b %H:%M"))
    return out
