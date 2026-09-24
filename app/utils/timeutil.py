"""Time helpers. Store UTC in the DB; convert for display/scheduling in Asia/Kuala_Lumpur."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from zoneinfo import ZoneInfo

MYT = ZoneInfo("Asia/Kuala_Lumpur")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_naive() -> datetime:
    """Naive UTC — the format stored in SQLite. Use for all DB comparisons."""
    return now_utc().replace(tzinfo=None)


def ensure_utc(dt: datetime) -> datetime:
    """DB datetimes are naive UTC; make them aware for arithmetic."""
    if dt is None:
        return dt
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def to_tz(dt: datetime, tz: ZoneInfo | None = None) -> datetime:
    tz = tz or MYT
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def my_now() -> datetime:
    return to_tz(now_utc(), MYT)


def parse_quiet_hours(spec: str) -> tuple[time, time]:
    """'23:00-08:00' -> (time(23,0), time(8,0))."""
    start_s, end_s = spec.split("-")
    sh, sm = (int(x) for x in start_s.strip().split(":"))
    eh, em = (int(x) for x in end_s.strip().split(":"))
    return time(sh, sm), time(eh, em)


def in_quiet_hours(dt: datetime | None = None, spec: str = "23:00-08:00") -> bool:
    dt = dt or my_now()
    start, end = parse_quiet_hours(spec)
    t = dt.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def hours_since(dt: datetime) -> float:
    dt = ensure_utc(dt)
    return (now_utc() - dt).total_seconds() / 3600.0


def add_hours(dt: datetime, hours: float) -> datetime:
    return dt + timedelta(hours=hours)
