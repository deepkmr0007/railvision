from __future__ import annotations

from datetime import date, datetime, timedelta

from railvision.core.models import LiveObservation, ObsKind, Route, ScheduledStop
from railvision.core.timeutil import IST

SD = date(2026, 10, 14)


def T(h, m, day=1):
    return datetime(2026, 10, 13 + day, h, m, tzinfo=IST)


def route(stops=None, is_demo=False, number="12951"):
    # Live-style fixture: A(halt) -> P1(pass) -> B(halt) -> RE(pass) -> C(halt) -> D(dest)
    stops = stops or [
        ScheduledStop(0, "AAA", "Alpha", 0, 1, None, "20:00", True),
        ScheduledStop(1, "PPA", "Pass One", 40, 1, None, "20:35", False),
        ScheduledStop(2, "BBB", "Bravo", 100, 1, "21:30", "21:35", True),
        ScheduledStop(3, "RE", "Rewari", 160, 1, None, "22:25", False),
        ScheduledStop(4, "CCC", "Charlie", 220, 1, "23:20", "23:25", True),
        ScheduledStop(5, "DDD", "Delta", 300, 2, "00:40", None, True),
    ]
    return Route(number, "TEST EXPRESS", stops, "test-src", is_demo=is_demo)


def ob(kind, code, when, src_lag=2, delay=None, number="12951", is_demo=False, **kw):
    return LiveObservation(train_number=number, service_date=SD, kind=ObsKind(kind), station_code=code,
                           event_time=when, source_id="test-src", source_time=(when or T(19, 0)) + timedelta(minutes=src_lag),
                           retrieved_at=(when or T(19, 0)) + timedelta(minutes=src_lag + 1), delay_min=delay,
                           is_demo=is_demo, **kw)
