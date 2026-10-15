"""Time handling. All Indian railway events are normalised to Asia/Kolkata (IST, UTC+05:30).

IST has no daylight saving, so a fixed offset is exact and avoids a tzdata dependency.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected: every railway timestamp must carry a timezone")
    return dt.astimezone(IST)


def parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    hh, mm = value.strip().split(":")[:2]
    return time(int(hh), int(mm))


def scheduled_dt(service_date: date, day: int, hhmm: str | None) -> datetime | None:
    """Absolute IST datetime of a timetable time.

    `service_date` is the date the train left its ORIGIN; `day` is 1 for the origin day,
    2 for the next calendar day, etc. This is how midnight crossings are handled correctly:
    a 00:15 time on day 2 is after a 23:50 time on day 1.
    """
    t = parse_hhmm(hhmm)
    if t is None:
        return None
    return datetime.combine(service_date + timedelta(days=day - 1), t, tzinfo=IST)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)  # sources documented as IST-local
    return dt.astimezone(IST)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(IST).isoformat(timespec="seconds") if dt else None


def fmt_hm(dt: datetime | None) -> str:
    return dt.astimezone(IST).strftime("%H:%M") if dt else "—"


def fmt_human(dt: datetime | None) -> str:
    return dt.astimezone(IST).strftime("%d %b %Y, %H:%M IST") if dt else "Unknown"


def minutes(td: timedelta) -> float:
    return td.total_seconds() / 60.0
