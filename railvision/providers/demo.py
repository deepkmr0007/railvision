"""SYNTHETIC demo provider — development and UI testing only.

* Train numbers 9000x and every name carry "(SYNTHETIC)"; `is_demo=True` on every object.
* Real station codes are used so the UI looks realistic, but timetables, kilometres,
  delays and berth charts are INVENTED and deterministic (seeded), not real data.
* The registry refuses to load this provider when RAILVISION_MODE=live.
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timedelta

from ..core.models import LiveObservation, ObsKind, Route, ScheduledStop, VacancySnapshot
from ..core.timeutil import IST, now_ist, scheduled_dt
from .base import NotSupported, ProviderAdapter, VERIFIED_NOW

# (code, name, km, rel_arr_min, rel_dep_min, halt, reported_by_source)
_BASE = [
    ("JP", "Jaipur Junction", 0, None, 0, True, True),
    ("GADJ", "Gandhinagar Jaipur", 5, None, 8, False, True),
    ("BKI", "Bandikui Junction", 91, 75, 77, True, True),
    ("AWR", "Alwar", 150, 125, 128, True, True),
    ("RE", "Rewari", 225, None, 185, False, False),      # non-stop & NOT reported by the source
    ("GGN", "Gurugram", 277, 228, 230, True, True),
    ("DEC", "Delhi Cantt", 290, None, 245, False, True),
    ("DLI", "Delhi Junction", 305, 270, None, True, True),
]

TRAINS = {
    "90001": {"name": "RAILVISION DEMO EXPRESS (SYNTHETIC)", "scenario": "normal", "reverse": False},
    "90002": {"name": "DEMO CANCELLED SPECIAL (SYNTHETIC)", "scenario": "cancelled", "reverse": False},
    "90003": {"name": "DEMO DIVERTED MAIL (SYNTHETIC)", "scenario": "diverted", "reverse": False},
    "90004": {"name": "DEMO RETURN EXPRESS (SYNTHETIC)", "scenario": "normal", "reverse": True},
}
DIVERSION_ROUTE = ["JP", "GADJ", "BKI", "AWR", "DVX1", "DEC", "DLI"]


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


class DemoProvider(ProviderAdapter):
    source_id = "demo-synthetic"
    display_name = "DEMO (synthetic fixtures)"
    is_demo = True
    capabilities = {k: VERIFIED_NOW + " (DEMO ONLY)" for k in
                    ("train_status", "train_route", "station_board", "trains_between", "chart_vacancy", "search")}

    def __init__(self, clock=now_ist, cycle_hours: int = 6, offset_min: int = 20, anchor: datetime | None = None):
        """`anchor` pins one fixed run per train (used for replays/tests); otherwise runs repeat every cycle."""
        self.clock, self.cycle_hours, self.offset_min, self.anchor = clock, cycle_hours, offset_min, anchor

    # ---- scenario timing -------------------------------------------------
    def current_departure(self, train_number: str) -> datetime:
        """Each demo train departs every `cycle_hours` so there is always something moving."""
        stagger = (int(train_number) % 4) * 25
        if self.anchor is not None:
            return self.anchor + timedelta(minutes=stagger)
        now = self.clock().astimezone(IST)
        block = now.replace(minute=0, second=0, microsecond=0)
        block -= timedelta(hours=block.hour % self.cycle_hours)
        dep = block + timedelta(minutes=self.offset_min + stagger)
        if dep > now:
            dep -= timedelta(hours=self.cycle_hours)
        return dep

    def _departure_for(self, train_number: str, service_date: date) -> datetime:
        cur = self.current_departure(train_number)
        if cur.date() == service_date:
            return cur
        return datetime.combine(service_date, cur.timetz())

    def default_service_date(self, train_number: str) -> date:
        return self.current_departure(train_number).date()

    def _layout(self, train_number: str):
        t = TRAINS.get(train_number)
        if not t:
            raise NotSupported(f"Demo provider has no synthetic train {train_number}. Demo trains: {', '.join(TRAINS)}")
        rows = list(_BASE)
        if t["reverse"]:
            total = rows[-1][2]
            end = rows[-1][3]
            rev = []
            for code, name, km, a, d, halt, rep in reversed(rows):
                if not halt:                      # run-through point: single passing time
                    na, nd = None, end - d
                elif code == "JP":                 # new destination
                    na, nd = end - d, None
                elif code == "DLI":                # new origin
                    na, nd = None, 0
                else:
                    na, nd = end - d, end - a
                rev.append((code, name, total - km, na, nd, halt, rep))
            rows = rev
        return t, rows

    # ---- adapter contract --------------------------------------------------
    def get_train_route(self, train_number: str, service_date: date) -> Route:
        t, rows = self._layout(train_number)
        dep = self._departure_for(train_number, service_date)
        stops = []
        for i, (code, name, km, a, d, halt, _) in enumerate(rows):
            def hm(rel):
                if rel is None:
                    return None, None
                x = dep + timedelta(minutes=rel)
                return x.strftime("%H:%M"), (x.date() - service_date).days + 1
            arr, day_a = hm(a)
            dpt, day_d = hm(d)
            stops.append(ScheduledStop(seq=i, code=code, name=name, km=float(km), day=day_a or day_d or 1,
                                       arr=arr, dep=dpt, halt=halt,
                                       platform=str(1 + _seed(train_number, code) % 6) if halt else None))
        return Route(train_number=train_number, train_name=t["name"], stops=stops, source_id=self.source_id,
                     is_demo=True, runs_on="Daily (synthetic)")

    def _delays(self, train_number: str, service_date: date, n: int) -> list[int]:
        rnd = random.Random(_seed(train_number, service_date))
        d, out = rnd.randint(0, 9), []
        for _ in range(n):
            out.append(d)
            d = max(-3, d + rnd.randint(-2, 7))
        return out

    def get_train_status(self, train_number: str, service_date: date) -> list[LiveObservation]:
        t, rows = self._layout(train_number)
        route = self.get_train_route(train_number, service_date)
        now = self.clock()
        dep = self._departure_for(train_number, service_date)
        rnd = random.Random(_seed("lag", train_number, service_date))
        obs: list[LiveObservation] = []

        def add(kind, code, when, delay=None, lag=None, **kw):
            lag = lag if lag is not None else rnd.randint(2, 5)
            src_t = when + timedelta(minutes=lag)
            if src_t <= now:
                obs.append(LiveObservation(train_number=train_number, service_date=service_date, kind=kind,
                                           station_code=code, event_time=when, source_id=self.source_id,
                                           source_time=src_t, retrieved_at=now, delay_min=delay, is_demo=True, **kw))

        if t["scenario"] == "cancelled":
            add(ObsKind.CANCELLED, None, dep - timedelta(hours=3), lag=0,
                payload={"detail": "Synthetic cancellation scenario"})
            return obs

        delays = self._delays(train_number, service_date, len(rows))
        last_t, last_i = None, None
        diverted = t["scenario"] == "diverted"
        for i, (code, name, km, a, d, halt, reported) in enumerate(rows):
            if diverted and code in ("RE", "GGN"):
                continue
            dl = delays[i]
            st = route.stops[i]
            if a is not None:
                at = st.arr_dt(service_date) + timedelta(minutes=dl)
                if reported:
                    add(ObsKind.ARRIVED, code, at, dl, payload={"platform": st.platform} if st.platform else {})
                last_t, last_i = at, i
            if d is not None:
                dt_ = st.dep_dt(service_date) + timedelta(minutes=dl)
                if reported:
                    add(ObsKind.PASSED if not halt else ObsKind.DEPARTED, code, dt_, dl)
                last_t, last_i = dt_, i
            if diverted and code == "AWR":
                add(ObsKind.DIVERTED, "AWR", dt_ + timedelta(minutes=1), dl, lag=1,
                    payload={"route": DIVERSION_ROUTE, "detail": "Synthetic diversion via DVX1 (skips RE, GGN)"})
                pass_t = dt_ + timedelta(minutes=70)
                add(ObsKind.PASSED, "DVX1", pass_t, dl + 4)
        # live position between stations (like "x km after STN")
        reported_obs = [o for o in obs if o.kind in (ObsKind.DEPARTED, ObsKind.PASSED) and o.station_code in [r[0] for r in rows]]
        if reported_obs and not any(o.kind == ObsKind.ARRIVED and o.station_code == rows[-1][0] for o in obs):
            lo = max(reported_obs, key=lambda o: o.event_time)
            i = [r[0] for r in rows].index(lo.station_code)
            if i + 1 < len(rows) and not diverted:
                nxt = rows[i + 1]
                seg_km = nxt[2] - rows[i][2]
                elapsed = (now - timedelta(minutes=3) - lo.event_time).total_seconds() / 60
                # actual running time of this segment = scheduled time adjusted by the delay change
                seg_t = ((nxt[3] if nxt[3] is not None else nxt[4]) + delays[i + 1]) - (rows[i][4] + delays[i])
                if 0 < elapsed < seg_t and seg_km > 0:
                    km_after = round(seg_km * elapsed / seg_t, 1)
                    if km_after >= 2:
                        add(ObsKind.POSITION, lo.station_code, now - timedelta(minutes=3), lo.delay_min, lag=2,
                            km_after=km_after)
        return obs

    def search_trains(self, query: str) -> list[dict]:
        q = query.lower().strip()
        return [{"number": n, "name": t["name"], "is_demo": True} for n, t in TRAINS.items()
                if q in n or q in t["name"].lower() or q in ("demo", "")]

    def find_trains_between(self, from_station: str, to_station: str, on: date) -> list[dict]:
        out = []
        for n in TRAINS:
            r = self.get_train_route(n, on)
            i, j = r.index_of(from_station), r.index_of(to_station)
            if i is not None and j is not None and i < j:
                a, b = r.stops[i], r.stops[j]
                if not a.halt or not b.halt:
                    continue
                t0, t1 = a.dep_dt(on), b.arr_dt(on)
                out.append({"number": n, "name": r.train_name, "from": a.code, "to": b.code,
                            "departure": t0.isoformat(), "arrival": t1.isoformat(),
                            "duration_min": int((t1 - t0).total_seconds() // 60),
                            "scenario": TRAINS[n]["scenario"], "is_demo": True})
        return out

    def get_station_board(self, station_code: str, start: datetime, end: datetime) -> list[dict]:
        out = []
        for n in TRAINS:
            sd = self.default_service_date(n)
            r = self.get_train_route(n, sd)
            s = r.stop(station_code)
            if not s:
                continue
            t = s.pass_dt(sd)
            if t and start <= t <= end:
                out.append({"number": n, "name": r.train_name, "station": station_code, "halt": s.halt,
                            "sched_arr": s.arr, "sched_dep": s.dep, "platform": s.platform,
                            "service_date": sd.isoformat(), "is_demo": True})
        return sorted(out, key=lambda x: x["sched_dep"] or x["sched_arr"] or "")

    def get_chart_vacancy(self, train_number: str, journey_date: date, boarding_station: str, travel_class: str):
        r = self.get_train_route(train_number, journey_date)
        if TRAINS[train_number]["scenario"] == "cancelled":
            raise NotSupported("No chart: train cancelled (synthetic)")
        halts = [s.code for s in r.stops if s.halt]
        if boarding_station not in halts:
            raise NotSupported(f"{boarding_station} is not a boarding point for this train")
        segs = list(zip(halts, halts[1:]))
        rnd = random.Random(_seed("chart", train_number, journey_date, travel_class))
        cls = travel_class.upper()
        layout = {"3A": ("B", 64, ["LB", "MB", "UB", "LB", "MB", "UB", "SL", "SU"]),
                  "SL": ("S", 72, ["LB", "MB", "UB", "LB", "MB", "UB", "SL", "SU"]),
                  "2A": ("A", 48, ["LB", "UB", "LB", "UB", "SL", "SU"])}.get(cls)
        if not layout:
            raise NotSupported(f"Class {cls} not in demo fixture (try 3A, 2A, SL)")
        prefix, nberths, pattern = layout
        coaches = []
        for c in range(1, 3):
            berths = []
            for b in range(1, nberths + 1):
                occ = [False] * len(segs)
                if rnd.random() < 0.8:                     # one passenger journey over a contiguous span
                    i = rnd.randrange(len(segs))
                    j = rnd.randrange(i, len(segs))
                    occ[i:j + 1] = [True] * (j - i + 1)
                berths.append({"no": b, "type": pattern[(b - 1) % len(pattern)],
                               "segments": [{"from": s[0], "to": s[1], "occupied": o} for s, o in zip(segs, occ)]})
            coaches.append({"coach": f"{prefix}{c}", "berths": berths})
        dep = self._departure_for(train_number, journey_date)
        return VacancySnapshot(train_number=train_number, journey_date=journey_date,
                               boarding_station=boarding_station, travel_class=cls, chart_no=1,
                               source_id=self.source_id, source_time=dep - timedelta(hours=4),
                               coaches=coaches, official_url="https://www.irctc.co.in/online-charts/",
                               is_demo=True, notes=["SYNTHETIC berth data for UI development — not a real chart."])

    def get_source_health(self) -> dict:
        return {"source_id": self.source_id, "status": "OK", "is_demo": True,
                "note": "Synthetic fixtures; never shown as live."}
