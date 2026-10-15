"""Normalised domain entities. Every provider adapter maps its payloads into these."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any

from .timeutil import iso, scheduled_dt


class ObsKind(str, Enum):
    ARRIVED = "ARRIVED"          # arrived at a station (halt)
    DEPARTED = "DEPARTED"        # departed a station (halt or origin)
    PASSED = "PASSED"            # ran through a non-halt station (granular passage evidence)
    POSITION = "POSITION"        # between stations, e.g. "x km after STN"
    CANCELLED = "CANCELLED"      # fully cancelled for this service date
    SHORT_TERMINATED = "SHORT_TERMINATED"
    DIVERTED = "DIVERTED"        # payload["route"] carries the new station sequence
    RESCHEDULED = "RESCHEDULED"  # payload["new_departure"]


class EventClass(str, Enum):
    SCHEDULED_HALT = "SCHEDULED_HALT"
    CONFIRMED_PASSAGE = "CONFIRMED_PASSAGE"
    ESTIMATED_CROSSING = "ESTIMATED_CROSSING"
    NOT_VERIFIED = "NOT_VERIFIED"
    NOT_ON_ROUTE = "NOT_ON_ROUTE"
    CANCELLED = "CANCELLED"


class Verification(str, Enum):
    VERIFIED_LIVE = "VERIFIED_LIVE"      # direct source observation
    DERIVED = "DERIVED"                  # logically implied by verified observations
    ESTIMATED = "ESTIMATED"              # model output with uncertainty
    STALE = "STALE"
    DEMO = "DEMO"                        # synthetic fixture: never shown as live


@dataclass
class Station:
    code: str
    name: str
    name_hi: str = ""
    lat: float | None = None
    lon: float | None = None
    aliases: tuple[str, ...] = ()


@dataclass
class ScheduledStop:
    seq: int
    code: str
    name: str
    km: float
    day: int = 1
    arr: str | None = None      # HH:MM IST, None at origin
    dep: str | None = None      # HH:MM IST, None at destination
    halt: bool = True           # False = scheduled run-through (non-stop) point
    platform: str | None = None  # only if the source supplies it

    @property
    def pass_time(self) -> str | None:
        return self.dep or self.arr

    # `day` is the day-of-run of the ARRIVAL (or departure at origin). A departure clock
    # time earlier than the arrival clock time means the dwell crossed midnight.
    def arr_dt(self, service_date):
        return scheduled_dt(service_date, self.day, self.arr)

    def dep_dt(self, service_date):
        extra = 1 if (self.arr and self.dep and self.dep < self.arr) else 0
        return scheduled_dt(service_date, self.day + extra, self.dep)

    def pass_dt(self, service_date):
        return self.dep_dt(service_date) if self.dep else self.arr_dt(service_date)


@dataclass
class Route:
    train_number: str
    train_name: str
    stops: list[ScheduledStop]
    source_id: str
    is_demo: bool = False
    runs_on: str = ""

    def index_of(self, code: str) -> int | None:
        for i, s in enumerate(self.stops):
            if s.code == code:
                return i
        return None

    def stop(self, code: str) -> ScheduledStop | None:
        i = self.index_of(code)
        return self.stops[i] if i is not None else None


@dataclass
class LiveObservation:
    train_number: str
    service_date: date
    kind: ObsKind
    station_code: str | None
    event_time: datetime | None          # when it happened (IST)
    source_id: str
    source_time: datetime                # when the source says this record was last updated
    retrieved_at: datetime               # when we fetched it
    delay_min: int | None = None
    km_after: float | None = None        # for POSITION observations
    is_demo: bool = False
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def dedupe_key(self) -> tuple:
        t = self.event_time.replace(second=0, microsecond=0) if self.event_time else None
        return (self.train_number, self.service_date, self.kind, self.station_code, t, self.km_after)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["service_date"] = self.service_date.isoformat()
        for k in ("event_time", "source_time", "retrieved_at"):
            d[k] = iso(getattr(self, k))
        return d


@dataclass
class CrossingResult:
    train_number: str
    service_date: date
    station_code: str
    station_name: str
    event_class: EventClass
    verification: Verification
    crossed: bool | None                 # True/False, None = unknown
    headline: str
    scheduled_time: datetime | None = None
    event_time: datetime | None = None   # exact confirmed time
    est_earliest: datetime | None = None
    est_latest: datetime | None = None
    est_central: datetime | None = None
    confidence: float = 0.0              # 0..1
    method: str = ""
    method_version: str = ""
    source_id: str | None = None
    source_time: datetime | None = None
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    is_demo: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_class"] = self.event_class.value
        d["verification"] = self.verification.value
        d["service_date"] = self.service_date.isoformat()
        for k in ("scheduled_time", "event_time", "est_earliest", "est_latest", "est_central", "source_time"):
            d[k] = iso(getattr(self, k))
        return d


@dataclass
class VacancySnapshot:
    train_number: str
    journey_date: date
    boarding_station: str
    travel_class: str
    chart_no: int | None
    source_id: str
    source_time: datetime
    coaches: list[dict]           # [{coach, berths:[{no,type,status,segments:[{from,to,occupied}]}]}]
    official_url: str
    is_demo: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["journey_date"] = self.journey_date.isoformat()
        d["source_time"] = iso(self.source_time)
        return d
